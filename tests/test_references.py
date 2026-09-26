# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

import pytest

from aiform.references import (
    Reference,
    ReferenceError,
    find_references,
    reference_targets,
    resolve,
)

WEB = "digitalocean.compute.web-01"
DB = "digitalocean.compute.db-01"
ZONE = "digitalocean.domain.example.com"

# The referenceable namespace is a target's state attributes plus `id` --
# `id` is not in `attributes` (orchestrator._pop_id() moves it to
# StateEntry.id), so callers merge it back in. These fixtures are shaped the
# way orchestrator.py will build them.
AVAILABLE = {
    WEB: {
        "id": "12345",
        "ipv4_address": "203.0.113.5",
        "region": "sfo3",
        "tags": ["aiform", "web"],
        "monitoring": True,
        "port_count": 2,
    },
    DB: {"id": "67890", "ipv4_address": "203.0.113.9"},
    ZONE: {"id": "example.com", "ttl": 1800},
}


def ref(text):
    return {"data": text}


class TestWholeValueReferencePreservesType:
    """A value that is exactly one reference substitutes the attribute's own
    value with its own type -- not a stringified copy of it. This is what
    lets a reference feed a non-string field."""

    def test_string_attribute_resolves_to_the_string(self):
        resolved, unresolved = resolve(ref(f"${{{WEB}:ipv4_address}}"), AVAILABLE)
        assert resolved == {"data": "203.0.113.5"}
        assert unresolved == []

    def test_int_attribute_stays_an_int(self):
        resolved, _ = resolve({"n": f"${{{WEB}:port_count}}"}, AVAILABLE)
        assert resolved == {"n": 2}
        assert isinstance(resolved["n"], int)

    def test_bool_attribute_stays_a_bool(self):
        resolved, _ = resolve({"m": f"${{{WEB}:monitoring}}"}, AVAILABLE)
        assert resolved == {"m": True}
        assert resolved["m"] is True

    def test_list_attribute_stays_a_list(self):
        resolved, _ = resolve({"t": f"${{{WEB}:tags}}"}, AVAILABLE)
        assert resolved == {"t": ["aiform", "web"]}

    def test_id_is_referenceable_even_though_it_is_not_in_attributes(self):
        resolved, _ = resolve(ref(f"${{{WEB}:id}}"), AVAILABLE)
        assert resolved == {"data": "12345"}


class TestEmbeddedReference:
    def test_reference_inside_a_larger_string_is_interpolated(self):
        resolved, _ = resolve(ref(f"web is ${{{WEB}:ipv4_address}} today"), AVAILABLE)
        assert resolved == {"data": "web is 203.0.113.5 today"}

    def test_several_references_in_one_string_all_substitute(self):
        resolved, _ = resolve(ref(f"${{{WEB}:ipv4_address}} and ${{{DB}:ipv4_address}}"), AVAILABLE)
        assert resolved == {"data": "203.0.113.5 and 203.0.113.9"}

    def test_embedded_int_is_stringified(self):
        resolved, _ = resolve(ref(f"count=${{{WEB}:port_count}}"), AVAILABLE)
        assert resolved == {"data": "count=2"}

    def test_embedding_a_non_scalar_raises(self):
        # Interpolating a list would produce "tags are ['aiform', 'web']" --
        # a Python repr, never what was meant. A whole-value reference to the
        # same attribute is legal; only embedding is refused.
        with pytest.raises(ReferenceError) as excinfo:
            resolve(ref(f"tags are ${{{WEB}:tags}}"), AVAILABLE)
        assert "tags" in str(excinfo.value)


class TestNesting:
    """References are found at arbitrary depth, because that is where they
    actually live: the canonical DNS case is params.records[0].data, and the
    firewall's sources.droplet_ids is two levels in."""

    def test_reference_inside_a_list_of_dicts(self):
        params = {"records": [{"type": "A", "data": f"${{{WEB}:ipv4_address}}"}]}
        resolved, _ = resolve(params, AVAILABLE)
        assert resolved == {"records": [{"type": "A", "data": "203.0.113.5"}]}

    def test_reference_inside_a_bare_list(self):
        resolved, _ = resolve({"ids": [f"${{{WEB}:id}}"]}, AVAILABLE)
        assert resolved == {"ids": ["12345"]}

    def test_reference_two_dicts_deep(self):
        params = {"outer": {"inner": {"data": f"${{{WEB}:ipv4_address}}"}}}
        resolved, _ = resolve(params, AVAILABLE)
        assert resolved == {"outer": {"inner": {"data": "203.0.113.5"}}}

    def test_path_spelling_indexes_lists_with_brackets(self):
        params = {"records": [{"data": "x"}, {"data": f"${{{WEB}:ipv4_address}}"}]}
        assert find_references(params) == {
            "records[1].data": [Reference(target_key=WEB, attribute="ipv4_address")]
        }


class TestDottedAndAwkwardKeys:
    def test_target_name_containing_dots_resolves(self):
        # digitalocean.domain.example.com is a real key: `name` may contain
        # dots, which is the whole reason the attribute is not a fourth
        # dotted segment.
        resolved, _ = resolve({"t": f"${{{ZONE}:ttl}}"}, AVAILABLE)
        assert resolved == {"t": 1800}

    def test_last_colon_separates_so_a_name_may_contain_a_colon(self):
        key = "digitalocean.compute.odd:name"
        available = {key: {"ipv4_address": "198.51.100.7"}}
        resolved, unresolved = resolve(ref(f"${{{key}:ipv4_address}}"), available)
        assert resolved == {"data": "198.51.100.7"}
        assert unresolved == []


class TestLiteralsPassThroughUntouched:
    """parser.py already anticipates a cloud-init `user_data: |` block scalar
    in params, and compute's PARAM_SCHEMA is additionalProperties: True, so
    shell parameter expansion in a params value is an anticipated case. A
    ${...} is a reference only when it holds a colon whose left side parses
    as a valid resource key."""

    @pytest.mark.parametrize(
        "text",
        [
            "${HOME}",
            "${PATH}",
            "${PORT:-8080}",
            "${FOO:+x}",
            "${digitalocean.compute.web-01:ipv4_address",
            "#!/bin/sh\necho ${HOME}/bin\n",
            "plain text with no dollar brace",
        ],
    )
    def test_literal_is_returned_unchanged(self, text):
        resolved, unresolved = resolve(ref(text), AVAILABLE)
        assert resolved == {"data": text}
        assert unresolved == []

    @pytest.mark.parametrize("text", ["${HOME}", "${PORT:-8080}"])
    def test_literal_contributes_no_reference_and_no_target(self, text):
        assert find_references(ref(text)) == {}
        assert reference_targets(ref(text)) == set()


class TestMalformedReferencesRaise:
    def test_dot_instead_of_colon_raises_naming_the_colon(self):
        # The likeliest user error, because it is what Terraform habits
        # produce. Passing it through would send the literal
        # "${digitalocean.compute.web-01.ipv4_address}" to the provider as a
        # DNS record's value.
        with pytest.raises(ReferenceError) as excinfo:
            resolve(ref("${digitalocean.compute.web-01.ipv4_address}"), AVAILABLE)
        assert "colon" in str(excinfo.value).lower()

    def test_bad_provider_in_an_otherwise_wellformed_reference_raises(self):
        with pytest.raises(ReferenceError):
            resolve(ref("${Digitalocean.compute.web-01:ipv4_address}"), AVAILABLE)

    def test_unknown_attribute_raises_and_lists_what_is_available(self):
        with pytest.raises(ReferenceError) as excinfo:
            resolve(ref(f"${{{WEB}:ipv4_addres}}"), AVAILABLE)
        message = str(excinfo.value)
        assert "ipv4_addres" in message
        # A typo'd attribute is otherwise indistinguishable from a driver
        # that stopped returning a field, so the real names must be shown.
        assert "ipv4_address" in message
        assert "region" in message

    def test_resolved_none_raises(self):
        # compute._flatten() returns ipv4_address: None when no public v4 is
        # attached (issue #178). A DNS record whose data is the string "None"
        # is worse than a refusal.
        available = {WEB: {"id": "1", "ipv4_address": None}}
        with pytest.raises(ReferenceError) as excinfo:
            resolve(ref(f"${{{WEB}:ipv4_address}}"), available)
        assert "ipv4_address" in str(excinfo.value)

    def test_the_error_names_the_param_path(self):
        params = {"records": [{"data": f"${{{WEB}:nope}}"}]}
        with pytest.raises(ReferenceError) as excinfo:
            resolve(params, AVAILABLE)
        assert "records[0].data" in str(excinfo.value)


class TestUnresolvedTargets:
    """A target absent from `available` is reported, not raised: it is the
    everyday first-apply case, where the dependency has not been created
    yet and the value is resolved during the apply loop instead."""

    def test_absent_target_is_returned_as_an_unresolved_path(self):
        resolved, unresolved = resolve(ref(f"${{{WEB}:ipv4_address}}"), {})
        assert unresolved == ["data"]

    def test_unresolved_path_keeps_its_literal_text(self):
        text = f"${{{WEB}:ipv4_address}}"
        resolved, _ = resolve(ref(text), {})
        assert resolved == {"data": text}

    def test_unresolved_paths_are_sorted(self):
        params = {"b": f"${{{WEB}:ipv4_address}}", "a": f"${{{DB}:ipv4_address}}"}
        _, unresolved = resolve(params, {})
        assert unresolved == ["a", "b"]

    def test_a_present_target_resolves_while_an_absent_one_does_not(self):
        params = {"here": f"${{{WEB}:ipv4_address}}", "gone": f"${{{DB}:ipv4_address}}"}
        resolved, unresolved = resolve(params, {WEB: AVAILABLE[WEB]})
        assert resolved["here"] == "203.0.113.5"
        assert unresolved == ["gone"]

    def test_an_unknown_attribute_on_an_absent_target_does_not_raise(self):
        # Nothing can be said about an attribute of a resource that does not
        # exist yet; the check happens when the target is available.
        _, unresolved = resolve(ref(f"${{{WEB}:whatever}}"), {})
        assert unresolved == ["data"]


class TestFindReferencesAndTargets:
    def test_find_references_reports_every_reference_at_a_path_in_order(self):
        params = ref(f"${{{WEB}:ipv4_address}} then ${{{DB}:ipv4_address}}")
        assert find_references(params) == {
            "data": [
                Reference(target_key=WEB, attribute="ipv4_address"),
                Reference(target_key=DB, attribute="ipv4_address"),
            ]
        }

    def test_paths_without_references_are_absent_rather_than_empty(self):
        params = {"plain": "x", "data": f"${{{WEB}:id}}"}
        assert list(find_references(params)) == ["data"]

    def test_reference_targets_deduplicates(self):
        params = {"a": f"${{{WEB}:id}}", "b": f"${{{WEB}:ipv4_address}}"}
        assert reference_targets(params) == {WEB}

    def test_reference_targets_of_empty_params_is_empty(self):
        assert reference_targets({}) == set()

    def test_find_references_of_empty_params_is_empty(self):
        assert find_references({}) == {}


class TestResolveDoesNotMutate:
    def test_input_params_are_left_alone(self):
        params = {"records": [{"data": f"${{{WEB}:ipv4_address}}"}]}
        before = f"${{{WEB}:ipv4_address}}"
        resolve(params, AVAILABLE)
        assert params["records"][0]["data"] == before

    def test_empty_params_resolve_to_an_equal_tree(self):
        resolved, unresolved = resolve({}, AVAILABLE)
        assert resolved == {}
        assert unresolved == []

    def test_non_string_scalars_are_passed_through(self):
        params = {"ttl": 3600, "on": True, "ratio": 1.5, "nothing": None}
        resolved, unresolved = resolve(params, AVAILABLE)
        assert resolved == params
        assert unresolved == []

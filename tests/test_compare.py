# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

import datetime

import pytest

from aiform.compare import canonical_key, unordered_equal


class TestUnorderedEqualLists:
    def test_list_of_strings_reordered_is_equal(self):
        assert unordered_equal(["a", "b", "c"], ["c", "a", "b"]) is True

    def test_list_of_strings_different_elements_is_not_equal(self):
        assert unordered_equal(["a", "b", "c"], ["a", "b", "d"]) is False

    def test_list_of_dicts_reordered_is_equal(self):
        # This is THE case that makes plain sorted() raise TypeError --
        # dicts aren't orderable, so unordered_equal must go through
        # canonical_key() rather than sorting the raw values directly.
        a = [{"name": "web", "type": "A"}, {"name": "mail", "type": "MX"}]
        b = [{"name": "mail", "type": "MX"}, {"name": "web", "type": "A"}]
        assert unordered_equal(a, b) is True

    def test_sorted_on_raw_dicts_raises_type_error(self):
        # Documents the reason canonical_key() exists at all: without it,
        # the obvious "just sort() them" implementation blows up on the
        # very input this feature is meant to handle.
        values = [{"name": "web", "type": "A"}, {"name": "mail", "type": "MX"}]
        with pytest.raises(TypeError):
            sorted(values)

    def test_dicts_with_differing_key_order_but_same_content_are_equal(self):
        assert unordered_equal([{"a": 1, "b": 2}], [{"b": 2, "a": 1}]) is True

    def test_duplicates_are_multiset_sensitive_not_set_sensitive(self):
        assert unordered_equal(["x", "x"], ["x"]) is False

    def test_true_and_one_are_not_equal(self):
        assert unordered_equal([True], [1]) is False

    def test_one_and_one_point_zero_are_not_equal(self):
        assert unordered_equal([1], [1.0]) is False

    def test_empty_lists_are_equal(self):
        assert unordered_equal([], []) is True

    def test_empty_list_and_nonempty_list_are_not_equal(self):
        assert unordered_equal([], ["a"]) is False

    def test_nested_lists_inside_elements_compare_positionally(self):
        # Only the top level of a declared field is order-insensitive --
        # a list nested inside an element is still compared in order,
        # because canonical_key() serializes it positionally.
        a = [{"x": [1, 2]}]
        b = [{"x": [2, 1]}]
        assert unordered_equal(a, b) is False

    def test_nested_lists_inside_elements_same_order_are_equal(self):
        a = [{"x": [1, 2]}]
        b = [{"x": [1, 2]}]
        assert unordered_equal(a, b) is True


class TestUnorderedEqualScalarFallback:
    def test_equal_scalars_are_equal(self):
        assert unordered_equal("web", "web") is True

    def test_scalar_vs_list_is_not_equal_and_does_not_raise(self):
        assert unordered_equal("web", ["web"]) is False

    def test_one_side_not_a_list_falls_back_to_equality_without_raising(self):
        assert unordered_equal(None, []) is False

    def test_both_none_are_equal(self):
        assert unordered_equal(None, None) is True

    def test_equal_dicts_not_wrapped_in_a_list_are_equal(self):
        assert unordered_equal({"a": 1}, {"a": 1}) is True


class TestCanonicalKey:
    def test_deterministic_across_calls(self):
        value = {"b": 2, "a": [1, {"z": 1, "y": 2}]}
        assert canonical_key(value) == canonical_key(value)

    def test_returns_a_string(self):
        assert isinstance(canonical_key({"a": 1}), str)

    def test_dict_key_order_does_not_affect_the_key(self):
        assert canonical_key({"a": 1, "b": 2}) == canonical_key({"b": 2, "a": 1})

    def test_true_and_one_produce_different_keys(self):
        assert canonical_key(True) != canonical_key(1)

    def test_one_and_one_point_zero_produce_different_keys(self):
        assert canonical_key(1) != canonical_key(1.0)

    def test_list_order_affects_the_key(self):
        assert canonical_key([1, 2]) != canonical_key([2, 1])


class TestCanonicalKeyTotality:
    """canonical_key must never raise on anything YAML can produce.

    The planner calls this inside diff_attributes(), so an exception
    here surfaces as an opaque failure on a plain `plan` -- and
    specs/unordered_fields.md is explicit that a malformed value must
    reach the driver's own clear error rather than being pre-empted by
    a TypeError from the diff. An earlier implementation used
    json.dumps' sort_keys=True, which sorts the raw keys and so raised
    TypeError on a mixed-type mapping.
    """

    def test_mixed_type_dict_keys_do_not_raise(self):
        # `1: x` in YAML is an int key, not the string "1".
        assert canonical_key({1: "a", "b": 2})

    def test_none_dict_key_does_not_raise(self):
        # `~: x` in YAML is a None key.
        assert canonical_key({None: 1, "a": 2})

    def test_unordered_equal_survives_mixed_type_keys(self):
        assert unordered_equal([{1: "a", "b": 2}], [{"b": 2, 1: "a"}])


class TestCanonicalKeyDoesNotHideDifferences:
    """The one direction this module must never fail in.

    unordered_equal's contract is that it never reports genuinely
    different inputs as equal. Two implementations have broken it:
    json.dumps' `default=str` made a date equal to the string that
    looks like it, and a later `str(key)` made a None key equal to the
    literal key "None".
    """

    def test_date_is_not_equal_to_the_string_that_looks_like_it(self):
        # yaml.safe_load parses an unquoted 2026-01-01 as a datetime.date,
        # so this collision was reachable from a user's aiform.md.
        assert not unordered_equal([datetime.date(2026, 1, 1)], ["2026-01-01"])

    @pytest.mark.parametrize(("key", "lookalike"), [(None, "None"), (True, "True")])
    def test_non_string_dict_key_is_not_equal_to_its_python_str_lookalike(self, key, lookalike):
        # str(None) is "None" but JSON's coercion is "null", so these are
        # different objects and must stay different. The earlier str(key)
        # implementation collapsed them.
        assert not unordered_equal([{key: 1}], [{lookalike: 1}])

    @pytest.mark.parametrize(("key", "wire"), [(None, "null"), (True, "true"), (1.0, "1.0")])
    def test_non_string_dict_key_IS_equal_to_its_json_wire_form(self, key, wire):
        # The deliberate counterpart to the test above, pinning the line
        # between the two. json.dumps({None: 1}) and json.dumps({"null": 1})
        # are both {"null":1}: a CSP receiving either sees an identical
        # request, so treating them as equal is correct rather than
        # diff-hiding. This is JSON's collision, not one aiform invented,
        # and canonicalizing toward the wire format is what makes it so.
        assert unordered_equal([{key: 1}], [{wire: 1}])

    @pytest.mark.parametrize(
        ("key", "encoded"),
        [(None, '"null"'), (True, '"true"'), (1, '"1"'), (1.0, '"1.0"')],
    )
    def test_dict_keys_use_json_coercion_not_python_str(self, key, encoded):
        # json.dumps({None: 1}) is {"null": 1} -- "null", not "None".
        # The spec claims JSON object-key semantics, so match them.
        assert encoded in canonical_key({key: 1})

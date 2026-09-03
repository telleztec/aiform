# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

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

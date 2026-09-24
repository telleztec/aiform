# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

import pytest

from aiform.graph import CycleError, topological_order


class TestTopologicalOrderBasicShapes:
    def test_no_edges_orders_alphabetically(self):
        # With nothing constraining order, sorted() draining of the ready
        # set is the only source of determinism -- this pins it directly.
        assert topological_order({"c", "a", "b"}, {}) == ["a", "b", "c"]

    def test_linear_chain_orders_dependency_before_dependent(self):
        # a depends on b, b depends on c: c must come first.
        edges = {"a": {"b"}, "b": {"c"}}
        assert topological_order({"a", "b", "c"}, edges) == ["c", "b", "a"]

    def test_disconnected_components_are_all_included(self):
        edges = {"a": {"b"}}
        result = topological_order({"a", "b", "x", "y"}, edges)
        assert set(result) == {"a", "b", "x", "y"}
        assert result.index("b") < result.index("a")

    def test_empty_graph_returns_empty_list(self):
        assert topological_order(set(), {}) == []


class TestTopologicalOrderFanIn:
    """A node with several dependencies -- fan-in -- is first-class, not a
    later widening: every one of its dependencies must precede it."""

    def test_all_dependencies_of_a_fan_in_node_precede_it(self):
        edges = {"app": {"db", "cache", "queue"}}
        result = topological_order({"app", "db", "cache", "queue"}, edges)
        assert result.index("db") < result.index("app")
        assert result.index("cache") < result.index("app")
        assert result.index("queue") < result.index("app")

    def test_fan_in_dependencies_with_no_edge_between_them_sort_deterministically(self):
        edges = {"app": {"db", "cache"}}
        assert topological_order({"app", "db", "cache"}, edges) == ["cache", "db", "app"]


class TestTopologicalOrderFanOut:
    """One node several others depend on -- the mirror of fan-in."""

    def test_a_shared_dependency_precedes_every_dependent(self):
        edges = {"web": {"base"}, "worker": {"base"}}
        result = topological_order({"web", "worker", "base"}, edges)
        assert result.index("base") < result.index("web")
        assert result.index("base") < result.index("worker")


class TestTopologicalOrderDiamond:
    def test_diamond_shape_orders_the_shared_base_first_and_apex_last(self):
        # top depends on {left, right}, both of which depend on base.
        edges = {"top": {"left", "right"}, "left": {"base"}, "right": {"base"}}
        result = topological_order({"top", "left", "right", "base"}, edges)
        assert result[0] == "base"
        assert result[-1] == "top"
        assert result.index("left") < result.index("top")
        assert result.index("right") < result.index("top")


class TestTopologicalOrderDuplicateTargets:
    def test_a_repeated_dependency_collapsing_into_one_set_element_has_no_extra_effect(self):
        # edges[k] is typed as a set, so a caller building it from a list
        # with a repeated target (["b", "b"]) collapses to one edge before
        # it ever reaches here -- this pins that the algorithm treats it
        # exactly like a single dependency, not double-counting in-degree.
        edges = {"a": set(["b", "b"])}
        assert topological_order({"a", "b"}, edges) == ["b", "a"]


class TestTopologicalOrderDeterminism:
    """Identical input must always yield identical output -- both the unit
    tests and reviewable `plan` output depend on it."""

    @pytest.mark.parametrize(
        "keys,edges",
        [
            (
                {"c", "a", "b", "app", "db", "cache"},
                {"app": {"db", "cache"}, "b": {"a"}},
            ),
            (
                {"app", "db", "cache", "a", "b", "c"},
                {"app": {"cache", "db"}, "b": {"a"}},
            ),
        ],
    )
    def test_same_logical_graph_from_differently_ordered_input_is_identical(self, keys, edges):
        first = topological_order(keys, edges)
        second = topological_order(set(keys), dict(edges))
        assert first == second

    def test_repeated_calls_on_the_same_input_are_identical(self):
        edges = {"app": {"db", "cache", "queue"}, "cache": {"queue"}}
        keys = {"app", "db", "cache", "queue"}
        results = [topological_order(keys, edges) for _ in range(5)]
        assert all(result == results[0] for result in results)


class TestTopologicalOrderIgnoresEdgesOutsideKeys:
    def test_an_edge_target_not_in_keys_is_ignored(self):
        edges = {"a": {"b", "not-in-this-run"}}
        assert topological_order({"a", "b"}, edges) == ["b", "a"]


class TestTopologicalOrderCycles:
    def test_self_dependency_is_a_length_one_cycle(self):
        with pytest.raises(CycleError) as exc_info:
            topological_order({"a"}, {"a": {"a"}})
        assert exc_info.value.path == ["a", "a"]

    def test_two_node_cycle_raises_cycle_error(self):
        with pytest.raises(CycleError):
            topological_order({"a", "b"}, {"a": {"b"}, "b": {"a"}})

    def test_multi_node_cycle_carries_a_walkable_path(self):
        # a depends on b, b depends on c, c depends on a.
        edges = {"a": {"b"}, "b": {"c"}, "c": {"a"}}
        with pytest.raises(CycleError) as exc_info:
            topological_order({"a", "b", "c"}, edges)
        path = exc_info.value.path
        assert path[0] == path[-1]
        for source, target in zip(path, path[1:], strict=False):
            assert target in edges[source]

    def test_cycle_among_a_subset_still_raises_even_with_acyclic_nodes_present(self):
        edges = {"a": {"b"}, "b": {"a"}, "x": set()}
        with pytest.raises(CycleError):
            topological_order({"a", "b", "x"}, edges)

    def test_acyclic_nodes_are_not_falsely_reported_as_part_of_the_cycle(self):
        edges = {"a": {"b"}, "b": {"a"}, "x": set()}
        with pytest.raises(CycleError) as exc_info:
            topological_order({"a", "b", "x"}, edges)
        assert "x" not in exc_info.value.path

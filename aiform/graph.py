# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

import heapq


class CycleError(Exception):
    """Raised by topological_order() when the graph is not acyclic.

    `path` is the cycle itself, as a walkable sequence -- path[i+1] is
    always in edges[path[i]] -- with path[0] == path[-1], so callers can
    render it directly (e.g. "a -> b -> c -> a") without re-deriving the
    walk. A node that merely depends on the cycle without being part of
    it never appears in `path`.
    """

    def __init__(self, path: list[str]):
        self.path = path
        super().__init__("dependency cycle: " + " -> ".join(path))


class UnknownDependencyError(Exception):
    """Raised by topological_order() when an edge names a target outside
    `keys`. `key` is the node declaring the edge, `target` the unresolved
    dependency it names.
    """

    def __init__(self, key: str, target: str):
        self.key = key
        self.target = target
        super().__init__(f"{key!r} depends on {target!r}, which is not in keys")


def topological_order(keys: set[str], edges: dict[str, set[str]]) -> list[str]:
    """Kahn's algorithm, ready set drained in sorted() order.

    `edges[k]` is the set of keys `k` depends on. Every target in every
    `edges[k]` must itself be in `keys` -- callers restrict edges to the
    keys under consideration before calling this, and that precondition is
    enforced here, not merely assumed: a target outside `keys` raises
    `UnknownDependencyError` rather than being dropped.

    Determinism is a requirement, not an accident: identical input must
    always produce identical output, since both the unit tests and
    reviewable `plan` output depend on it.
    """
    in_degree = dict.fromkeys(keys, 0)
    dependents: dict[str, set[str]] = {key: set() for key in keys}
    for key in keys:
        for dependency in edges.get(key, set()):
            if dependency not in keys:
                raise UnknownDependencyError(key, dependency)
            in_degree[key] += 1
            dependents[dependency].add(key)

    ready = [key for key in keys if in_degree[key] == 0]
    heapq.heapify(ready)

    order: list[str] = []
    while ready:
        current = heapq.heappop(ready)
        order.append(current)
        for dependent in dependents[current]:
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                heapq.heappush(ready, dependent)

    if len(order) != len(keys):
        raise CycleError(_walk_cycle(keys - set(order), edges))
    return order


def _walk_cycle(remaining: set[str], edges: dict[str, set[str]]) -> list[str]:
    # No separate cycle detector: after Kahn's terminates, `remaining` is
    # the cycle(s) plus every node that transitively depends on one --
    # not the cycle itself. But every node left in it still has at least
    # one dependency inside it (otherwise Kahn's would have drained it),
    # so walking dependency edges from any starting node inside
    # `remaining` is guaranteed to loop back on some node eventually. The
    # walk may pass through lead-in nodes before it does; the genuine
    # cycle is the suffix starting at the first repeated node.
    start = min(remaining)
    path = [start]
    visited = {start}
    current = start
    while True:
        candidates = (dep for dep in edges.get(current, set()) if dep in remaining)
        current = min(candidates)
        path.append(current)
        if current in visited:
            return path[path.index(current) :]
        visited.add(current)

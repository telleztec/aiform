# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

import heapq


class CycleError(Exception):
    """Raised by topological_order() when the graph is not acyclic.

    `path` is the cycle as a walkable sequence -- path[i+1] is always in
    edges[path[i]] -- with path[0] == path[-1], so callers can render it
    directly (e.g. "a -> b -> c -> a") without re-deriving the walk.
    """

    def __init__(self, path: list[str]):
        self.path = path
        super().__init__("dependency cycle: " + " -> ".join(path))


def topological_order(keys: set[str], edges: dict[str, set[str]]) -> list[str]:
    """Kahn's algorithm, ready set drained in sorted() order.

    `edges[k]` is the set of keys `k` depends on. A key in `edges` that is
    not in `keys` is ignored -- callers restrict edges to the keys under
    consideration before calling, and this does not second-guess them.

    Determinism is a requirement, not an accident: identical input must
    always produce identical output, since both the unit tests and
    reviewable `plan` output depend on it.
    """
    in_degree = dict.fromkeys(keys, 0)
    dependents: dict[str, set[str]] = {key: set() for key in keys}
    for key in keys:
        for dependency in edges.get(key, set()):
            if dependency not in keys:
                continue
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
    # No separate cycle detector: after Kahn's terminates, `remaining` IS
    # the cycle (possibly with more than one cycle tangled together), and
    # every node left in it still has at least one dependency inside it --
    # otherwise Kahn's would have drained it. Walking dependency edges
    # from any starting node inside `remaining` is guaranteed to loop back
    # on itself.
    start = min(remaining)
    path = [start]
    visited = {start}
    current = start
    while True:
        candidates = (dep for dep in edges.get(current, set()) if dep in remaining)
        current = min(candidates)
        path.append(current)
        if current in visited:
            return path
        visited.add(current)

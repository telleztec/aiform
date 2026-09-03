# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

import json
from typing import Any


def canonical_key(value: Any) -> str:
    """Total, deterministic ordering key for any YAML/JSON-derived value.

    `sort_keys=True` matters because the two sides of a diff don't come
    from the same serializer: one side's dicts are parsed from the
    user's YAML-ish aiform.md, the other's from the CSP's JSON response,
    and neither format guarantees a stable key order. Without sorting,
    `{"a": 1, "b": 2}` and `{"b": 2, "a": 1}` would produce different
    keys and be treated as different elements, even though they're the
    same value.

    `default=str` keeps this total (never raises) for anything the
    `json` module can't serialize natively. In practice params only ever
    contain str/int/float/bool/None/list/dict, so this branch shouldn't
    fire -- it's a safety net, not a feature.

    Deliberately does NOT normalize types that the CSP would treat as
    distinct: `True` and `1` produce different keys (`"true"` vs `"1"`),
    and so do `1` and `1.0` (`"1"` vs `"1.0"`). Collapsing those would
    let genuinely different values compare equal.
    """
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))


def unordered_equal(a: Any, b: Any) -> bool:
    """Multiset equality for two lists; falls back to == otherwise.

    Multiset, not set: `set()` semantics would report `["x", "x"]` equal
    to `["x"]`, silently swallowing a duplicated tag or record. Multiset
    comparison never reports genuinely different inputs as equal, so it
    errs toward surfacing a diff rather than hiding one.

    Falls back to plain `==` when either side isn't a list (including
    when both are scalars, or one/both are None) so a malformed or
    scalar value never raises here -- raising a clear error for that
    case is the driver's job, not the diff's.

    Only the top level is unordered: elements are compared via
    `canonical_key()`, which serializes any list nested inside an
    element positionally. A declared field's own order never matters;
    what's inside its elements still does.
    """
    if not isinstance(a, list) or not isinstance(b, list):
        return a == b
    return sorted(map(canonical_key, a)) == sorted(map(canonical_key, b))

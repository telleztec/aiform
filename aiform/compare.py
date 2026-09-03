# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

import json
from typing import Any

# Types json.dumps serializes natively and unambiguously. bool is a
# subclass of int but serializes as true/false, so True and 1 still
# produce different keys -- which is what we want, since a CSP may well
# treat them differently.
_JSON_NATIVE = (str, int, float, bool, type(None))

# Marker for a value json cannot represent natively (a date parsed from
# unquoted YAML, a Decimal, a tuple). Tagging with the type name keeps
# such a value distinguishable from a plain string that happens to look
# like it -- see _canonical.
_NON_JSON_TAG = "__aiform_nonjson__"


def _canonical(value: Any) -> Any:
    """Rewrite `value` into a form json.dumps handles totally and unambiguously.

    Two hazards this exists to close, both found in review:

    Dict keys are sorted here by `str(k)` rather than by json.dumps'
    own `sort_keys=True`. That flag sorts the raw keys, so it raises
    TypeError on a dict with mixed-type keys -- and YAML produces those
    readily: `1: x` gives an int key, `~: x` a None key. A planner that
    dies with a TypeError mid-diff is precisely what specs/unordered_fields.md
    forbids; the driver is supposed to raise the clear error instead.
    Keys are also stringified, so {1: "x"} and {"1": "x"} collapse --
    matching JSON's own object-key semantics rather than inventing new ones.

    Non-JSON-native values are tagged with their type name rather than
    passed through json.dumps' `default=str`. Bare stringification made
    `datetime.date(2026, 1, 1)` and the string "2026-01-01" compare
    EQUAL while plain `==` called them different -- a diff-hiding bug,
    the one direction this module must never fail in. YAML parses an
    unquoted 2026-01-01 as a date, so that collision was reachable.
    """
    if isinstance(value, dict):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda kv: str(kv[0]))
        }
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    if isinstance(value, _JSON_NATIVE):
        return value
    return [_NON_JSON_TAG, type(value).__name__, str(value)]


def canonical_key(value: Any) -> str:
    """Total, deterministic ordering key for any YAML/JSON-derived value.

    Total in the strict sense: it never raises, for any input, including
    the mixed-key dicts and non-serializable leaves _canonical describes.
    Callers depend on that -- this runs inside the planner's diff, where
    an exception would surface as an opaque failure on a plain `plan`.

    Deliberately does NOT merge values a CSP could treat as distinct:
    True vs 1 produce "true" vs "1", and 1 vs 1.0 produce "1" vs "1.0".
    """
    return json.dumps(_canonical(value), separators=(",", ":"))


def unordered_equal(a: Any, b: Any) -> bool:
    """Multiset equality for two lists; falls back to == otherwise.

    Multiset, not set: `set()` semantics would report ["x", "x"] equal
    to ["x"], silently swallowing a duplicated tag or record. Multiset
    comparison never reports genuinely different inputs as equal, so it
    errs toward surfacing a diff rather than hiding one.

    Falls back to plain `==` when either side isn't a list (including
    when both are scalars, or one/both are None) so a malformed or
    scalar value never raises here -- raising a clear error for that
    case is the driver's job, not the diff's. Note this fallback is by
    type, not by shape: a driver whose read() returns a tuple rather
    than a list gets ordered comparison and no warning that its
    UNORDERED_FIELDS declaration didn't take. Drivers return lists;
    flagged in specs/unordered_fields.md rather than silently widened,
    because accepting tuples here would make ["a"] equal ("a",) and
    that is the diff-hiding direction.

    Only the top level is unordered: elements are compared via
    canonical_key(), which serializes any list nested inside an element
    positionally. A declared field's own order never matters; what's
    inside its elements still does.
    """
    if not isinstance(a, list) or not isinstance(b, list):
        return a == b
    return sorted(map(canonical_key, a)) == sorted(map(canonical_key, b))

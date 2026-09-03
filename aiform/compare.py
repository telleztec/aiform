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


def _dict_key(key: Any) -> str:
    """Coerce a dict key the way json.dumps does, for every key type it accepts.

    Beyond those it does not follow json.dumps, which *raises* TypeError
    on a key it cannot coerce (a date, bytes, a tuple). Such a key is
    tagged instead, for the same reason non-JSON-native values are --
    raising here would crash the planner's diff, and YAML yields a
    `datetime.date` key from an unquoted `2026-01-01:`. The tagged form
    is distinct from the string that looks like it, so this errs toward
    surfacing a diff.

    Not `str(key)`. That looks equivalent and isn't: `str(None)` is
    "None" but JSON's is "null", and `str(True)` is "True" against
    JSON's "true". Using Python's spelling made `{None: 1}` compare
    EQUAL to `{"None": 1}` -- two objects a CSP would never conflate,
    since neither is what goes on the wire. Found in review, one round
    after `default=str` caused the same class of bug for values.

    The residual collision -- `{None: 1}` and `{"null": 1}` now share a
    key -- is JSON's own, not one invented here: both serialize to
    `{"null":1}`, so a CSP receiving either sees the same request. That
    makes treating them as equal correct rather than diff-hiding, which
    is the whole reason to canonicalize toward the wire format instead
    of toward Python's repr.
    """
    if isinstance(key, str):
        return key
    return canonical_key(key)


def _canonical(value: Any) -> Any:
    """Rewrite `value` into a form json.dumps handles totally and unambiguously.

    Dict keys are coerced and sorted here rather than by json.dumps'
    own `sort_keys=True`. That flag sorts the raw keys, so it raises
    TypeError on a dict with mixed-type keys -- and YAML produces those
    readily: `1: x` gives an int key, `~: x` a None key. A planner that
    dies with a TypeError mid-diff is precisely what
    specs/unordered_fields.md forbids; the driver is supposed to raise
    the clear error instead.

    Non-JSON-native values are tagged with their type name rather than
    passed through json.dumps' `default=str`. Bare stringification made
    `datetime.date(2026, 1, 1)` and the string "2026-01-01" compare
    EQUAL while plain `==` called them different -- a diff-hiding bug,
    the one direction this module must never fail in. YAML parses an
    unquoted 2026-01-01 as a date, so that collision was reachable.
    """
    if isinstance(value, dict):
        return {
            coerced: _canonical(item)
            for coerced, item in sorted(
                ((_dict_key(key), item) for key, item in value.items()),
                key=lambda kv: kv[0],
            )
        }
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    if isinstance(value, _JSON_NATIVE):
        return value
    return [_NON_JSON_TAG, type(value).__name__, str(value)]


def canonical_key(value: Any) -> str:
    """Total, deterministic ordering key for any YAML/JSON-derived value.

    Total over the acyclic values YAML and JSON produce -- including the
    mixed-type keys and non-serializable leaves _canonical describes.
    Callers depend on that: this runs inside the planner's diff, where
    an exception surfaces as an opaque failure on a plain `plan`.

    Deliberately NOT claimed to be total in the absolute sense, which an
    earlier version of this docstring did assert. A self-referential
    structure still raises RecursionError, and `yaml.safe_load` -- the
    same loader aiform/parser.py uses -- will build one from an anchor
    that references itself (`tags: &t [*t]`). That input is beyond
    repair here rather than merely awkward: it has no finite
    serialization, so no canonical key exists to return.

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

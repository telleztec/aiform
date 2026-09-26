# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

import re
from collections.abc import Mapping
from typing import Any, NamedTuple

from aiform.models import RESOURCE_OR_PROVIDER_PATTERN, parse_dependency_key

# Every `${...}` in a params value, whatever it turns out to mean. Splitting
# "is this a reference" from "find the braces" is what lets shell text like
# ${HOME} or ${PORT:-8080} survive: compute's PARAM_SCHEMA is
# additionalProperties: True and parser.py already anticipates a cloud-init
# `user_data: |` block scalar, so a params value holding shell is an
# anticipated case, not a hypothetical. `[^{}]*` refuses nesting rather than
# guessing at it.
_BRACED_RE = re.compile(r"\$\{([^{}]*)\}")

_ATTRIBUTE_RE = re.compile(RESOURCE_OR_PROVIDER_PATTERN)

# A `${...}` with no colon whose content looks like a resource key: the
# dot-instead-of-colon typo, which is what Terraform habits produce. Narrow
# on purpose -- ${HOME} has no dots and ${PORT:-8080} has a colon, so neither
# can reach this.
_KEYLIKE_RE = re.compile(
    rf"^{RESOURCE_OR_PROVIDER_PATTERN[1:-1]}\.{RESOURCE_OR_PROVIDER_PATTERN[1:-1]}\..+$"
)

_INTERPOLATABLE = (str, int, float, bool)


class Reference(NamedTuple):
    target_key: str
    attribute: str


class ReferenceResolutionError(Exception):
    """A reference that cannot be honoured.

    Not named `ReferenceError`: that is a Python builtin (weakrefs), so
    `except ReferenceError:` in a module that forgot the import would silently
    catch the wrong exception.

    `path` is the dotted params path it was found at, which is the only thing
    that locates it for a user -- a reference lives inside nested `params`
    (`records[0].data`), so naming the resource alone is not enough.
    """

    def __init__(self, path: str, message: str):
        self.path = path
        super().__init__(f"{path}: {message}")


def _looks_like_a_key(key: str) -> bool:
    """Whether the text left of the colon was *trying* to be a resource key.

    This is the line between "literal text, leave it alone" and "a reference
    the user got wrong, refuse it", and it has to be drawn on intent rather
    than validity -- a shell expansion is also `${...:...}`. One dot is the
    whole test, because a shell variable name cannot contain one: `${PORT:-8080}`,
    `${var:1:3}` and `${VERSION:-1.2.3}` all leave a dotless key and stay
    literal, while anything dotted is not valid shell syntax to begin with.

    Deliberately *not* also checking that the first segment is a well-formed
    provider: `${Digitalocean.compute.web-01:ipv4_address}` is plainly an
    attempted reference with a capitalised provider, and treating it as literal
    text would send it to the provider verbatim -- the exact failure this check
    exists to prevent. It is validated below instead, so it raises.
    """
    return "." in key


def _as_reference(content: str, path: str) -> Reference | None:
    """Classify one `${<content>}`. None means "literal text, leave it".

    Raises once the content is evidently an attempted reference, rather than
    passing it through: the alternative is sending the literal
    `${digitalocean.compute.web:ipv4_address}` to the provider as a DNS
    record's value, which is the failure this whole check exists to prevent.
    """
    # rpartition, not partition: `name` may contain a colon, so the LAST one
    # separates. A name containing dots is the reason the attribute is not a
    # fourth dotted segment in the first place.
    key, separator, attribute = content.rpartition(":")
    if not separator:
        if _KEYLIKE_RE.match(content):
            raise ReferenceResolutionError(
                path,
                f"{content!r} looks like a resource reference but has no colon before "
                "its attribute -- write ${provider.resource_type.name:attribute}",
            )
        return None

    if not _looks_like_a_key(key):
        return None
    try:
        parse_dependency_key(key)
    except ValueError as exc:
        raise ReferenceResolutionError(path, str(exc)) from exc
    if not _ATTRIBUTE_RE.match(attribute):
        # An attribute-side typo used to fall through as a literal, which sent
        # `${...:ipv4-address}` to the provider verbatim. The key half already
        # parsed, so intent is not in doubt.
        raise ReferenceResolutionError(
            path,
            f"{attribute!r} is not a valid attribute name; expected lowercase letters, "
            "digits and underscores, starting with a letter",
        )
    return Reference(target_key=key, attribute=attribute)


def _references_in(text: str, path: str) -> list[Reference]:
    return [
        reference
        for match in _BRACED_RE.finditer(text)
        if (reference := _as_reference(match.group(1), path)) is not None
    ]


def _walk(value: Any, path: str):
    """Yield (path, text) for every string in the tree, lists indexed with
    brackets so a path round-trips as something a user can find in their
    file."""
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk(child, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, f"{path}[{index}]")
    elif isinstance(value, str):
        yield path, value


def find_references(params: dict[str, Any]) -> dict[str, list[Reference]]:
    found = {}
    for path, text in _walk(params, ""):
        references = _references_in(text, path)
        if references:
            found[path] = references
    return found


def reference_targets(params: dict[str, Any]) -> set[str]:
    return {
        reference.target_key
        for references in find_references(params).values()
        for reference in references
    }


def _walk_pairs(raw: Any, resolved: Any, path: str):
    """Walk the raw and resolved trees together, keyed on the raw tree's
    shape, yielding (path, resolved value) for every string in `raw`.

    Driven by `raw` rather than by `resolved` because the resolved side alone
    is ambiguous: `{"t": "${...:tags}"}` resolves to a list at path `t`, while
    `{"ids": ["${...:id}"]}` has its reference at `ids[0]`. Walking the
    resolved tree would have to guess which list came *from* a reference and
    which merely *contains* one.
    """
    if isinstance(raw, dict):
        for key, child in raw.items():
            child_resolved = resolved.get(key) if isinstance(resolved, dict) else None
            yield from _walk_pairs(child, child_resolved, f"{path}.{key}" if path else str(key))
    elif isinstance(raw, list):
        for index, child in enumerate(raw):
            child_resolved = (
                resolved[index] if isinstance(resolved, list) and index < len(resolved) else None
            )
            yield from _walk_pairs(child, child_resolved, f"{path}[{index}]")
    elif isinstance(raw, str):
        yield path, resolved


def describe(
    params: dict[str, Any], resolved: dict[str, Any]
) -> list[tuple[str, list[Reference], Any]]:
    """(path, the references at it, what that path resolved to), sorted by
    path -- what the plan display and --json are rendered from.

    The value is per *path*, not per reference: several references embedded in
    one string share the single string they produced. An unresolved path
    carries its literal text, since that is what `resolve()` left there.
    """
    found = find_references(params)
    values = dict(_walk_pairs(params, resolved, ""))
    return [(path, found[path], values.get(path)) for path in sorted(found)]


def _attribute_value(reference: Reference, attributes: Mapping[str, Any], path: str) -> Any:
    try:
        value = attributes[reference.attribute]
    except KeyError:
        available = ", ".join(sorted(attributes)) or "(none)"
        raise ReferenceResolutionError(
            path,
            f"{reference.target_key} has no attribute {reference.attribute!r}; "
            f"available: {available}",
        ) from None
    if value is None:
        # compute._flatten() returns ipv4_address: None before a public v4
        # attaches (#178). A DNS record whose data is the string "None" is
        # worse than a refusal.
        raise ReferenceResolutionError(
            path,
            f"{reference.target_key}'s {reference.attribute!r} is not set yet "
            "(the provider reported no value)",
        )
    return value


def _resolve_text(
    text: str, path: str, available: Mapping[str, Mapping[str, Any]]
) -> tuple[Any, bool]:
    """Returns (value, resolved). `value` is `text` unchanged when not
    resolved, so an unresolved path keeps something a user recognises."""
    references = _references_in(text, path)
    if not references:
        return text, True

    if any(reference.target_key not in available for reference in references):
        return text, False

    whole = _BRACED_RE.fullmatch(text)
    if whole is not None and len(references) == 1:
        # The entire value is one reference, so the attribute's own type
        # survives -- an int stays an int, a list stays a list. This is what
        # lets a reference feed a non-string field.
        return _attribute_value(references[0], available[references[0].target_key], path), True

    def substitute(match: re.Match) -> str:
        reference = _as_reference(match.group(1), path)
        if reference is None:
            return match.group(0)
        value = _attribute_value(reference, available[reference.target_key], path)
        if not isinstance(value, _INTERPOLATABLE):
            raise ReferenceResolutionError(
                path,
                f"{reference.target_key}'s {reference.attribute!r} is a "
                f"{type(value).__name__} and cannot be substituted into a larger "
                "string; reference it as the whole value instead",
            )
        return str(value)

    return _BRACED_RE.sub(substitute, text), True


def _resolve_value(
    value: Any, path: str, available: Mapping[str, Mapping[str, Any]], unresolved: list[str]
) -> Any:
    if isinstance(value, dict):
        return {
            key: _resolve_value(child, f"{path}.{key}" if path else str(key), available, unresolved)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [
            _resolve_value(child, f"{path}[{index}]", available, unresolved)
            for index, child in enumerate(value)
        ]
    if isinstance(value, str):
        resolved, ok = _resolve_text(value, path, available)
        if not ok:
            unresolved.append(path)
        return resolved
    return value


def resolve(
    params: dict[str, Any], available: Mapping[str, Mapping[str, Any]]
) -> tuple[dict[str, Any], list[str]]:
    unresolved: list[str] = []
    resolved = _resolve_value(params, "", available, unresolved)
    return resolved, sorted(unresolved)

# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

from abc import ABC, abstractmethod
from typing import Any


class DriverUpdateNotSupported(Exception):
    """Raised by update() when this SPECIFIC diff cannot be applied
    in-place against the live API. The orchestrator catches this and
    falls back to delete() + create() — this is the deliberate
    replacement for Terraform's static, per-attribute ForceNew flag:
    the decision is made per-call against the real diff, not declared
    once and for all up front."""

    def __init__(self, reason: str, unsupported_fields: list[str] | None = None):
        self.reason = reason
        self.unsupported_fields = unsupported_fields or []
        super().__init__(reason)


class ResourceDriver(ABC):
    """
    The contract every (provider, resource) driver implements. The
    orchestrator dynamically imports a driver module, instantiates its
    `Driver` class, and calls these four methods identically regardless
    of provider or resource kind — this class is what makes that
    genericity a structural property, not a convention.
    """

    # Declares the `params` shape this driver accepts. Used by the
    # orchestrator to validate a parsed aiform.md spec before ever
    # calling create()/update(), and shown to Opus at generation-review
    # time as ground truth for what the driver claims to handle.
    PARAM_SCHEMA: dict[str, Any]

    # Optional, advisory only (never authoritative — update() is the
    # real arbiter). Populated at generation time so `aiform plan` can
    # WARN that a change is likely to force a replace, purely for UX,
    # without pretending to know for certain the way Terraform's
    # ForceNew does. Subclasses that don't override it get an empty list.
    # This default list is a shared class attribute: a subclass must
    # reassign it (`LIKELY_REPLACE_FIELDS = [...]`), never mutate it in
    # place, or it corrupts every other driver still inheriting the
    # base's empty list.
    LIKELY_REPLACE_FIELDS: list[str] = []

    # PARAM_SCHEMA keys read() structurally cannot recover from the CSP
    # (a write-only field only accepted at creation time, never returned
    # by any subsequent GET) — authoritative, not advisory like
    # LIKELY_REPLACE_FIELDS above. This class itself does nothing with
    # the list; it's read by orchestrator.py's refresh_resource(), which
    # carries a prior state entry's value for such a key forward across
    # every read()-driven refresh instead of letting the necessarily
    # incomplete response blank it out. This is NOT a diff-exclusion
    # mechanism: neither planner.py's diff_attributes() nor a driver's
    # own update() should skip these keys in their comparisons — an
    # earlier version did exactly that and it silently dropped a
    # genuine, intended change to the field (reverted after /code-review
    # caught it; see specs/driver.md and specs/digitalocean_compute.md).
    # Carrying the value forward keeps the plain comparison correct on
    # both sides: unchanged -> still matches -> no-op; changed -> diffs
    # against the last-known value -> reaches update(), which then
    # raises DriverUpdateNotSupported for a field it can't apply live.
    # Same shared-class-attribute reassign-don't-mutate rule as
    # LIKELY_REPLACE_FIELDS.
    NON_DIFFABLE_FIELDS: list[str] = []

    # PARAM_SCHEMA keys whose real-world semantics are a collection with
    # no meaningful order -- a set or multiset, not a sequence -- so the
    # planner should compare them via aiform.compare.unordered_equal()
    # instead of ordered equality. Exists because a CSP is free to
    # return the same elements in a different order than the user
    # declared them (e.g. DigitalOcean's tags), and plain != treats that
    # as a permanent, non-empty diff, which in turn permanently defeats
    # the zero-LLM-call short-circuit for that resource. NOT inferred
    # from PARAM_SCHEMA's `type: array`: plenty of legitimately ordered
    # fields are arrays too (a boot-script sequence, a priority list), so
    # a driver must opt in explicitly per field rather than have order-
    # sensitivity guessed at. Same shared-class-attribute rule as
    # LIKELY_REPLACE_FIELDS and NON_DIFFABLE_FIELDS above: a subclass
    # reassigns it (`UNORDERED_FIELDS = [...]`), never mutates it in
    # place, or it corrupts every other driver still inheriting the
    # base's empty list.
    UNORDERED_FIELDS: list[str] = []

    @abstractmethod
    def create(
        self, name: str, params: dict[str, Any], credentials: dict[str, str]
    ) -> dict[str, Any]:
        """
        Create the resource via the CSP API.

        name: the resource's `name:` field from aiform.md -- the primary
            key in state ("<provider>.<resource>.<name>"), and typically
            also the identifying label/hostname the CSP itself wants at
            creation time (e.g. a DigitalOcean droplet's "name"). Passed
            separately from `params` because it is a distinct top-level
            frontmatter field, never nested inside `params:`.
        params: the resource's `params` block from aiform.md, already
            validated by the orchestrator against PARAM_SCHEMA before
            this is ever called.
        credentials: e.g. {"DIGITALOCEAN_TOKEN": "..."}. Resolved by
            aiform/config.py. Never logged, never passed through any
            Anthropic API call.

        Returns: dict with at least {"id": str, **attributes} reflecting
            the resource as actually created. Becomes the state entry's
            initial `attributes`.
        """

    @abstractmethod
    def read(self, id: str, credentials: dict[str, str]) -> dict[str, Any]:
        """
        Fetch current live attributes for `id`.

        Returns: dict, same attribute shape as create()'s return.
        Raises: aiform.exceptions.ResourceNotFoundError if the resource
            no longer exists on the CSP side (signals drift).
        """

    @abstractmethod
    def update(
        self, id: str, current: dict[str, Any], desired: dict[str, Any], credentials: dict[str, str]
    ) -> dict[str, Any]:
        """
        Attempt to reconcile `current` -> `desired` in place.

        Inspects the ACTUAL diff (not a static per-field flag) and
        decides per-call whether an in-place update is possible. E.g.
        for a compute resource: resizing UP may be a live resize
        action; resizing DOWN may require powering off first (the
        driver may do this automatically within the call); some fields
        (e.g. a droplet's base image) are never in-place-updatable.

        Returns: dict of attributes after the update (same shape as
            create()).
        Raises: DriverUpdateNotSupported when THIS diff can't be
            applied in place. The orchestrator catches this, treats
            the operation as a "replace" (delete() then create()), and
            — if this wasn't already flagged as a likely replace
            during planning — pauses for the single-resource Opus
            safety gate before proceeding.

            Other exceptions may legitimately propagate uncaught, and
            must NOT be converted into DriverUpdateNotSupported: a
            transient CSP failure (rate limiting, a 5xx, an auth
            problem) is not evidence that this diff is unsupported, and
            misclassifying it as such would trigger a destructive
            replace for an update that might have succeeded on retry.
            Only an error that specifically means "the CSP rejected
            this diff as invalid" (not "the request failed for some
            other reason") should become DriverUpdateNotSupported — see
            drivers/digitalocean/compute.py's update() for a worked
            example (caught by /code-review after an earlier version
            of that driver got this wrong).

            ORDERING REQUIREMENT. Never raise DriverUpdateNotSupported
            after having mutated anything other than a power state this
            call itself restores. The orchestrator answers this
            exception by asking the review-orchestration-model and then
            the user for permission to replace, and the user may say
            no — in which case apply_plan() returns aborted, having
            written nothing to state. Anything already changed on the
            CSP side is then live and untracked. A driver that applies
            several fields in one update() must therefore attempt the
            field that can still be refused FIRST, before touching any
            other. drivers/digitalocean/compute.py orders its resize
            ahead of its tag and backup steps for exactly this reason.
        """

    @abstractmethod
    def delete(self, id: str, credentials: dict[str, str]) -> None:
        """
        Destroy the resource. MUST be idempotent: a 404 from the CSP
        (resource already gone) is treated as success, not an error.
        """

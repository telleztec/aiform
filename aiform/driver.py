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
        """

    @abstractmethod
    def delete(self, id: str, credentials: dict[str, str]) -> None:
        """
        Destroy the resource. MUST be idempotent: a 404 from the CSP
        (resource already gone) is treated as success, not an error.
        """

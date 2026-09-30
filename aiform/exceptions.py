# SPDX-FileCopyrightText: 2026 Juan Tellez
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path


class ResourceNotFoundError(Exception):
    """Raised by a ResourceDriver's read() when the resource no longer
    exists on the provider's side (PLAN.md §4/§5) — the orchestrator's
    refresh step catches this by name to mark drifted_missing rather
    than treating a deleted resource as an unhandled error."""


class DriverExecutionError(Exception):
    """Raised by orchestrator.py when a driver call raises anything other
    than the exception types the driver contract documents (PLAN.md §4's
    "Orchestrator invocation contract") — a raw CSP API failure, wrapped
    for uniform CLI error formatting."""

    def __init__(self, provider: str, resource_type: str, operation: str, original: Exception):
        self.provider = provider
        self.resource_type = resource_type
        self.operation = operation
        self.original = original
        super().__init__(f"{provider}.{resource_type} driver failed during {operation}: {original}")


class PlanBlockedError(Exception):
    """Raised by orchestrator.py whenever a plan cannot proceed for a
    policy reason -- a missing driver, a missing credential, or a gate #2
    review that didn't approve (PLAN.md §5)."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class DeploymentMismatchError(Exception):
    """Raised by state.load() when the state file at `path` belongs to a
    different deployment than the one the caller asked to act on (#201)."""

    def __init__(self, requested: str, found: str, path: Path):
        self.requested = requested
        self.found = found
        self.path = path
        super().__init__(
            f"this state file belongs to deployment {found!r}, not {requested!r}.\n"
            f"  state file: {path}\n"
            "  Nothing was read from the provider and nothing was changed."
        )

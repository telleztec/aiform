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
    policy reason -- a missing/untrusted driver, a missing credential, or
    a gate #1/#2 review that didn't approve (PLAN.md §5)."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)

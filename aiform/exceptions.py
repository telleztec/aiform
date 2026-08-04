class ResourceNotFoundError(Exception):
    """Raised by a ResourceDriver's read() when the resource no longer
    exists on the provider's side (PLAN.md §4/§5) — the orchestrator's
    refresh step catches this by name to mark drifted_missing rather
    than treating a deleted resource as an unhandled error."""

"""Errors that are safe to expose through the local API."""


class PipelineError(RuntimeError):
    """Expected user-facing workflow error."""


class ConflictError(PipelineError):
    """The requested state transition conflicts with an active run."""

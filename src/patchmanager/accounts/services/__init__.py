"""Business logic for identity and access. Views call these, never models directly."""


class ServiceError(Exception):
    """A rule was violated; the message is safe to show to the user."""


class PermissionDeniedError(ServiceError):
    """The actor lacks a required permission."""

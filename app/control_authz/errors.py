"""Unauthenticated and unauthorized failures. Default deny."""


class AuthnError(Exception):
    """The caller did not present a server-recognized credential."""

    code = "NOT_AUTHORIZED"
    http = 403

    def __init__(self, message: str = "unauthenticated"):
        super().__init__(message)
        self.message = message


class AuthzError(Exception):
    """The authenticated principal is not allowed to perform the operation."""

    code = "NOT_AUTHORIZED"
    http = 403

    def __init__(self, message: str = "unauthorized"):
        super().__init__(message)
        self.message = message

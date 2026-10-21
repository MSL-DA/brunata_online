"""The error types this client raises.

Every failure that leaves the API package is one of these four, and the
coordinator in __init__.py translates each into the Home Assistant error that
matches it. Kept in a module of its own so that the parser, the HTTP layer and
the login flow can all raise them without importing one another.
"""

from __future__ import annotations

class BrunataError(Exception):
    """Base class for every error this client raises."""


class BrunataAuthError(BrunataError):
    """Credentials were rejected. The user has to re-authenticate."""


class BrunataConnectionError(BrunataError):
    """The server could not be reached. Worth retrying later."""


class BrunataApiError(BrunataError):
    """The server answered, but not with something usable.

    Carries the HTTP status when there was one, so a caller can tell 429 from
    503 from 404 without parsing the message; diagnostics.py reports it. None
    for failures with no status of their own — a login flow that changed
    shape, a locale resource without mappers.

    `retry_after` is set only for 429, from the header of the same name, and is
    how long Brunata asked us to wait. None when it did not say.
    """

    def __init__(
        self,
        message: str,
        status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after

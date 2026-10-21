"""Reading an HTTP response from Brunata.

The status codes that have their own meaning, the error body Brunata sends
with HTTP 200, and the Retry-After header. Everything here works on a response
that has already arrived; sending the request is the client's job.
"""

from __future__ import annotations

import logging
import math
from typing import Any

import httpx

from .errors import BrunataApiError, BrunataAuthError

_LOGGER = logging.getLogger(__name__)

def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Read the Retry-After header, in its delay-seconds form.

    RFC 9110 also allows an HTTP-date. That form is not read here: it needs the
    server's clock to agree with ours to be worth anything, and getting it
    wrong means waiting either far too long or not at all. Falling back to a
    fixed default is the safer failure, and the coordinator does that.

    The finiteness check is not decoration. float() accepts "inf", "Infinity"
    and "1e400", and inf is greater than zero, so without it such a header
    passes straight through to timedelta(seconds=...) in the coordinator —
    which raises OverflowError from inside the very except block that exists to
    translate this error, and it escapes as an unexpected exception. A header
    that is not a real number is no header at all, same as the date form.

    A number that is merely enormous is left to the caller: the coordinator
    caps it, because how long we are willing to stay away is its decision, not
    the parser's.
    """
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        seconds = float(str(raw).strip())
    except ValueError:
        _LOGGER.debug("Brunata sent an unreadable Retry-After: %r", raw)
        return None
    if not math.isfinite(seconds):
        _LOGGER.debug("Brunata sent a non-finite Retry-After: %r", raw)
        return None
    return seconds if seconds > 0 else None


def _payload(response: httpx.Response) -> Any:
    """Decode a JSON response, raising on anything that isn't usable."""
    status = response.status_code

    if status == 429:
        raise BrunataApiError(
            f"Brunata rate limit reached ({status})",
            status,
            _retry_after_seconds(response),
        )
    if status >= 500:
        raise BrunataApiError(f"Brunata server error ({status})", status)
    if status == 404:
        raise BrunataApiError(f"Brunata endpoint not found ({status})", status)

    try:
        payload = response.json()
    except ValueError as err:
        raise BrunataApiError(f"Brunata returned invalid JSON: {err}", status) from err

    # Brunata reports some errors with HTTP 200 and an error body.
    if isinstance(payload, dict) and (
        payload.get("errorCode") is not None or payload.get("errorMessage") is not None
    ):
        code = payload.get("errorCode")
        message = payload.get("errorMessage")
        if code == "WB_WEBSERVICES_0011" or (
            isinstance(message, str) and "Not authorized" in message
        ):
            raise BrunataAuthError(f"Brunata authentication failed: {message}")
        raise BrunataApiError(f"Brunata returned error {code}: {message}", status)

    return payload

"""Sending a request to Brunata and reading what comes back.

async_request() is the one place a transport failure becomes
BrunataConnectionError, and both BrunataApiClient and BrunataAuth go through
it. The rest works on a response that has already arrived: the status codes
that have their own meaning, the error body Brunata sends with HTTP 200, and
the Retry-After header.
"""

from __future__ import annotations

import logging
import math
from typing import Any

import httpx

from .errors import BrunataApiError, BrunataAuthError, BrunataConnectionError

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


async def async_request(
    client: httpx.AsyncClient, method: str, url: str, **kwargs: Any
) -> httpx.Response:
    """Perform a request, mapping transport failures to our own error.

    httpx.RequestError, not httpx.TransportError. Read off httpx's own
    exception hierarchy rather than inferred from the names:

        RequestError
          + TransportError
            - TimeoutException, NetworkError, ProtocolError,
              ProxyError, UnsupportedProtocol
          + DecodingError
          + TooManyRedirects
        HTTPStatusError
        InvalidURL          <- outside RequestError

    RequestError adds exactly two cases over TransportError, and both mean
    the same thing as the rest: the request produced no usable response.
    TooManyRedirects can only reach us from _async_authorize(), the one
    call made with follow_redirects=True, and DecodingError from a broken
    content-encoding somewhere in the path. Caught as TransportError only,
    both escaped this module untranslated and reached the coordinator as
    unexpected exceptions with a traceback, instead of as the clean retry
    that keeps the last known values.

    Not httpx.HTTPError: InvalidURL sits outside RequestError, so a URL
    this integration built wrong keeps failing loudly instead of being
    disguised as a network problem.
    """
    try:
        return await client.request(method, url, **kwargs)
    except httpx.RequestError as err:
        raise BrunataConnectionError(f"Cannot reach Brunata: {err}") from err

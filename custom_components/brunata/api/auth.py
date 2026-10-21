"""The parts of the Keycloak login that are not requests.

The login flow itself is driven by BrunataApiClient, which owns the HTTP
client and the tokens. What lives here is everything that flow needs but can
decide without the network: the regular expression that finds the login form,
the check on where that form posts to, reading the authorization code back out
of a redirect, and reading a token lifetime.
"""

from __future__ import annotations

import html
import logging
import math
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

from .const import KC_REALM_BASE
from .errors import BrunataApiError

_LOGGER = logging.getLogger(__name__)

# Keycloak renders the login form as <form id="kc-form-login" ... action="...">
_KC_FORM_ACTION_RE = re.compile(
    r'id="kc-form-login"[^>]*action="([^"]+)"', re.IGNORECASE
)


# The email and password are posted to whatever address that action carries, so
# the address is checked against this host first. See _checked_form_action().
# Derived from KC_REALM_BASE rather than written out again: one spelling of
# Brunata's host, and it follows the realm URL if that ever moves.
_KC_HOST = urlparse(KC_REALM_BASE).hostname


_REDIRECT_STATUSES = (301, 302, 303, 307, 308)


# Renew slightly before the server's own expiry, so a slow request or a little
# clock drift doesn't send a token that expires mid-flight.
_EXPIRY_MARGIN_SECONDS = 30


def _expires_in_seconds(raw: Any) -> float | None:
    """Read a token lifetime, or None when it cannot be read.

    This exists so that _store_tokens() does not call float() on the raw value
    directly. A string that is not a number raises ValueError and a list raises
    TypeError, and both callers catch only BrunataApiError around that call —
    so either one would go straight out of _async_login(), out of
    async_get_meters() and into the coordinator as an unexpected exception
    rather than as a clean retry or reauth.

    None means "no usable expiry", which _store_tokens() already treats as an
    immediately unusable token: the next request logs in again. Failing that
    way costs one login; failing the other way costs the update.

    Bools are refused for the same reason _meter_type_code() refuses them.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        _LOGGER.debug("Brunata sent an unreadable expires_in: %r", raw)
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        _LOGGER.debug("Brunata sent an unreadable expires_in: %r", raw)
        return None
    if not math.isfinite(seconds):
        _LOGGER.debug("Brunata sent a non-finite expires_in: %r", raw)
        return None
    return seconds


def _checked_form_action(raw: str) -> str:
    """Return the login form's action URL, refusing one that is not Brunata's.

    The email and password go to whatever address Keycloak wrote into the form.
    The Location check further down runs on the answer to that POST, so by then
    the password has already been sent; this is the only point where a wrong
    destination can still be refused in time.

    Scheme, host and port only. A path check was considered and left out: an
    attacker who can write into the page Brunata serves is not stopped by it,
    and it would break the login the day Keycloak is upgraded and its
    login-actions path moves. The observed action already sits outside the
    realm base — /iam/login-actions/... rather than under KC_REALM_BASE — so
    the path is not stable enough to assert on.

    A relative action has no hostname and is refused here. It would otherwise
    reach httpx as a URL with no host and raise httpx.InvalidURL, which
    _async_request() deliberately does not translate: the same event, one
    traceback later.

    BrunataApiError, not BrunataAuthError, for the same reason as the missing
    form in _async_authorize(): nothing here says the password is wrong, and a
    reauth dialog would ask for one that works.
    """
    action = html.unescape(raw)
    parsed = urlparse(action)
    try:
        port = parsed.port
    except ValueError:
        # urlparse only validates the port when the attribute is read, so a
        # malformed one raises here rather than above. -1 is not a port, which
        # is the point: it fails the check below and shows up in the message.
        port = -1

    if parsed.scheme != "https" or parsed.hostname != _KC_HOST or port not in (None, 443):
        # The scheme, host and port, never the whole URL: the action carries
        # Keycloak's one-time session_code, and this message reaches both the
        # log and the diagnostics download, which get attached to public
        # issues. The port is included only when it is the thing that failed,
        # so a rejected port cannot be mistaken for a rejected host.
        where = f"{parsed.scheme or '?'}://{parsed.hostname or '?'}"
        if port not in (None, 443):
            where = f"{where}:{port}"
        raise BrunataApiError(f"Brunata login form posts to {where} — the login flow has changed")
    return action


def _code_from_url(url: Any) -> str | None:
    """Pull the OAuth authorization code out of a redirect URL."""
    return parse_qs(urlparse(str(url)).query).get("code", [None])[0]

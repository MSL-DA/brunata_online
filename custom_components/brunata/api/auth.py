"""The Keycloak login: the flow, the tokens, and the checks around them.

BrunataAuth owns one account's credentials and the tokens minted for them, and
is the only thing in the package that talks to Keycloak. BrunataApiClient holds
one and asks it for an Authorization header; it never reads a token itself.

Three requests at most, and usually none:

    a usable access token          no request at all
    an expired one, refresh held   one POST to the token endpoint
    neither, or a forced login      authorize, credentials, code exchange

The free functions below are the parts the flow needs but can decide without
the network — finding the login form, checking where it posts to, reading the
authorization code back out of a redirect, and reading a token lifetime.
"""

from __future__ import annotations

import base64
import hashlib
import html
import logging
import math
import re
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from .const import (
    KC_AUTHORIZE_URL,
    KC_CLIENT_ID,
    KC_REALM_BASE,
    KC_REDIRECT_URI,
    KC_SCOPE,
    KC_TOKEN_URL,
)
from .errors import BrunataApiError, BrunataAuthError
from .http import _payload, async_request

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
class BrunataAuth:
    """Holds one account's credentials and the tokens minted for them."""

    def __init__(self, email: str, password: str, http_client: httpx.AsyncClient) -> None:
        """Take the HTTP client rather than build one.

        The client is owned by BrunataApiClient, which closes it on unload.
        This class only borrows it — and it has to be the same one, because
        Keycloak's SSO cookies live on it and a forced login clears them.
        """
        self._email = email
        self._password = password
        self._client = http_client

        self._access_token: str | None = None
        self._token_type: str = "Bearer"
        self._refresh_token: str | None = None
        self._expires_at: float = 0.0

    # --- What the rest of the package uses ----------------------------------

    async def async_authorization(self, *, force: bool = False) -> str:
        """Ensure a usable token and return the Authorization header value.

        The finished header rather than the token and its type separately, so
        the two are read in one place and cannot be paired up wrongly
        somewhere else.

        `force` means the server rejected a token this class believed was
        good. See async_ensure_token().
        """
        await self.async_ensure_token(force=force)
        return f"{self._token_type} {self._access_token}"

    def diagnostics(self) -> dict[str, Any]:
        """Report whether tokens are held, for the diagnostics download.

        Whether, never which. An access token is a working credential, and
        diagnostics files get attached to public issues.
        """
        return {
            "has_access_token": self._access_token is not None,
            "has_refresh_token": self._refresh_token is not None,
        }

    async def _async_request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Perform a request on the borrowed HTTP client.

        The translation of a transport failure into BrunataConnectionError
        lives in http.async_request(), because this class and
        BrunataApiClient both need it and neither should reach into the other
        for it.
        """
        return await async_request(self._client, method, url, **kwargs)

    # --- The flow -----------------------------------------------------------

    @property
    def _token_is_usable(self) -> bool:
        return bool(self._access_token) and time.time() < self._expires_at

    def _store_tokens(self, payload: Any) -> None:
        """Record a token response.

        Every field is replaced rather than merged. Merging let an old expiry
        survive a response that carried none, so an expired token could be
        reported as usable and the login it needed was skipped.

        The payload is checked for being a JSON object first, the same check
        _async_ensure_lookup_tables() makes on the locale resource and for the
        same reason. _payload() reads the status and looks for an error body,
        but a body that is a list, a string or a bare null passes through it
        untouched — and .get() on one of those raises AttributeError, which is
        not a type either caller catches. It escaped _async_login(), escaped
        async_get_meters() and reached the coordinator as an unexpected
        exception with a traceback instead of a clean retry.

        The traceback is the smaller half. It escaped from inside
        _async_try_refresh() *before* the return that falls back to a full
        login, so the same rejected refresh token was replayed every hour
        until the entry was reloaded. BrunataApiError is the type both callers
        already know what to do with.
        """
        if not isinstance(payload, dict):
            raise BrunataApiError(
                "Brunata returned a token response that is not an object: "
                f"{type(payload).__name__}"
            )

        access_token = payload.get("access_token")
        if not access_token:
            raise BrunataApiError("Brunata returned no access token")

        self._access_token = access_token
        self._token_type = payload.get("token_type") or "Bearer"
        self._refresh_token = payload.get("refresh_token")

        expires_in = _expires_in_seconds(payload.get("expires_in"))
        self._expires_at = (
            time.time() + expires_in - _EXPIRY_MARGIN_SECONDS
            if expires_in is not None
            else 0.0
        )

    def _clear_tokens(self) -> None:
        self._access_token = None
        self._refresh_token = None
        self._expires_at = 0.0

    async def async_ensure_token(self, *, force: bool = False) -> None:
        """Ensure a usable access token, doing as little work as possible."""
        if not force:
            if self._token_is_usable:
                return
            if await self._async_try_refresh():
                return
        else:
            # The server rejected a token we believed was good. Keycloak's SSO
            # cookies live on this same client, so keeping them would just mint
            # another token from the very session that was refused — and the
            # refresh token from that session is no better.
            self._clear_tokens()
            self._client.cookies.clear()

        await self._async_browser_login()

    async def _async_try_refresh(self) -> bool:
        """Redeem the refresh token. Returns False to fall back to a login.

        We request the offline_access scope, so Keycloak issues one. Using it
        turns a renewal into a single POST instead of a three-request browser
        login, which is faster and far less likely to trip bot protection.
        """
        if not self._refresh_token:
            return False

        response = await self._async_request(
            "POST",
            KC_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": KC_CLIENT_ID,
                "refresh_token": self._refresh_token,
            },
            follow_redirects=False,
        )
        if response.status_code != 200:
            _LOGGER.debug(
                "Brunata refresh token rejected (%s) — falling back to a full login",
                response.status_code,
            )
            return False

        try:
            self._store_tokens(_payload(response))
        except BrunataApiError:
            return False

        _LOGGER.debug("Brunata access token renewed via refresh token")
        return True

    async def _async_browser_login(self) -> None:
        """Run the OAuth 2.0 Authorization Code + PKCE flow by hand."""
        # secrets.token_urlsafe uses only the unreserved characters RFC 7636
        # allows, and always lands inside its 43-128 character range.
        code_verifier = secrets.token_urlsafe(64)
        code_challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )

        auth_code = await self._async_authorize(code_challenge)
        response = await self._async_request(
            "POST",
            KC_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "client_id": KC_CLIENT_ID,
                "redirect_uri": KC_REDIRECT_URI,
                "code": auth_code,
                "code_verifier": code_verifier,
            },
            follow_redirects=False,
        )
        if response.status_code != 200:
            self._clear_tokens()
            raise BrunataAuthError(
                f"Brunata rejected the authorization code ({response.status_code})"
            )

        try:
            self._store_tokens(_payload(response))
        except BrunataApiError as err:
            self._clear_tokens()
            raise BrunataAuthError(str(err)) from err

    async def _async_authorize(self, code_challenge: str) -> str:
        """Obtain an authorization code, logging in if we have to."""
        page = await self._async_request(
            "GET",
            KC_AUTHORIZE_URL,
            params={
                "client_id": KC_CLIENT_ID,
                "redirect_uri": KC_REDIRECT_URI,
                "scope": KC_SCOPE,
                "response_type": "code",
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            },
            follow_redirects=True,
        )
        if page.status_code >= 400:
            raise BrunataApiError(
                f"Brunata authorize endpoint returned {page.status_code}",
                page.status_code,
            )

        # A live Keycloak SSO session redirects straight to the redirect URI
        # with ?code=... and renders no form. That is success: treating the
        # missing form as an error would prompt for credentials that work.
        if auth_code := _code_from_url(page.url):
            _LOGGER.debug("Brunata SSO session still active — no login form needed")
            return auth_code

        # Not a BrunataAuthError. Nothing here says the credentials are wrong;
        # it says Keycloak rendered something this code no longer recognises.
        # An auth error would open reauth and ask for a password that is
        # perfectly valid, and the next attempt would fail in the same place.
        # An API error becomes UpdateFailed, so HA retries and the log says
        # what actually broke.
        match = _KC_FORM_ACTION_RE.search(page.text)
        if not match:
            raise BrunataApiError(
                "Brunata login form not found — the login flow has changed"
            )

        auth = await self._async_request(
            "POST",
            _checked_form_action(match.group(1)),
            data={
                "username": self._email,
                "password": self._password,
                "credentialId": "",
            },
            follow_redirects=False,
        )
        # On success Keycloak issues a redirect; on failure it re-renders the
        # form with an error message.
        if auth.status_code not in _REDIRECT_STATUSES:
            raise BrunataAuthError(
                "Brunata rejected the login — check the email and password"
            )

        # Both mean the credentials were accepted — Keycloak issued a redirect
        # — and that what came back is not the shape this code expects. Same
        # reasoning as the missing form above: an API error, not a prompt for
        # credentials that already worked.
        location = auth.headers.get("Location", "")
        if not location.startswith(KC_REDIRECT_URI):
            raise BrunataApiError(f"Unexpected redirect after login: {location}")

        auth_code = _code_from_url(location)
        if not auth_code:
            raise BrunataApiError("Brunata returned no authorization code")

        # Logged because the alternative routes each say so — the refresh
        # ("access token renewed via refresh token") and the SSO shortcut
        # ("SSO session still active"). Without this line a full login was the
        # only path that produced no output at all, so a log could not tell
        # "the refresh token is working" from "we log in from scratch every
        # hour" — which is the difference between two requests per poll and
        # four, and the one that matters to a bot-protected endpoint.
        _LOGGER.debug("Brunata login form accepted the credentials")
        return auth_code

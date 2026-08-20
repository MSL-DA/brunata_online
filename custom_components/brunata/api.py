"""Client for the Brunata Online API.

This replaces the external ``brunata-api`` package. That package targeted
Brunata's retired Azure AD B2C login and v1 data API, so the integration had to
monkey-patch ``Client._get_tokens`` and rebind the module-level ``API_URL`` at
import time, then reach into ``_session``, ``_username``, ``_password``,
``_tokens``, ``_is_token_valid``, ``_meters`` and ``_init_mappers``. Every one
of those is private, so any release of the library could have broken the
integration at runtime with no warning.

Everything the integration actually used is implemented here instead: the
Keycloak login and the single ``/consumer/meters`` call. The HTTP client comes
from Home Assistant, which builds the SSL context off the event loop and closes
the client at shutdown.
"""

from __future__ import annotations

import base64
import hashlib
import html
import logging
import re
import secrets
import time
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from homeassistant.core import HomeAssistant
from homeassistant.helpers.httpx_client import create_async_httpx_client

_LOGGER = logging.getLogger(__name__)

# Brunata retired the v1 data API alongside the Keycloak migration: with a
# valid token, /online-webservice/v1/rest/consumer/meters answers 401 while v2
# returns the meter data.
API_URL = "https://online.brunata.com/online-webservice/v2/rest"

# Sent as the Referer on API calls, matching what the web app does.
METERS_URL = "https://online.brunata.com/consumption-overview"

KC_REALM_BASE = (
    "https://online.brunata.com/iam/realms/online-prod/protocol/openid-connect"
)
KC_AUTHORIZE_URL = f"{KC_REALM_BASE}/auth"
KC_TOKEN_URL = f"{KC_REALM_BASE}/token"
KC_CLIENT_ID = "82770188-c92e-4d16-927d-a15c472eda55"
KC_REDIRECT_URI = "https://online.brunata.com/auth-redirect"
KC_SCOPE = "openid offline_access"

# Keycloak renders the login form as <form id="kc-form-login" ... action="...">
_KC_FORM_ACTION_RE = re.compile(
    r'id="kc-form-login"[^>]*action="([^"]+)"', re.IGNORECASE
)
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)

# Renew slightly before the server's own expiry, so a slow request or a little
# clock drift doesn't send a token that expires mid-flight.
_EXPIRY_MARGIN_SECONDS = 30

REQUEST_TIMEOUT = 15.0


class BrunataError(Exception):
    """Base class for every error this client raises."""


class BrunataAuthError(BrunataError):
    """Credentials were rejected. The user has to re-authenticate."""


class BrunataConnectionError(BrunataError):
    """The server could not be reached. Worth retrying later."""


class BrunataApiError(BrunataError):
    """The server answered, but not with something usable."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True, slots=True)
class BrunataMeter:
    """A single meter and its most recent reading.

    Flat by design: the previous library nested the reading in its own object,
    which meant every consumer had to guard against a meter existing without
    one. Here an absent reading is simply ``value is None``.
    """

    meter_id: str
    meter_no: str | None
    meter_type: str
    unit: str
    value: float | None = None
    reading_date: date | None = None


def _parse_reading_date(raw: Any) -> date | None:
    """Parse Brunata's reading date, tolerating a full timestamp."""
    if isinstance(raw, str):
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            _LOGGER.debug("Could not parse reading date %r", raw)
    return None


class BrunataApiClient:
    """Talks to Brunata Online on behalf of one account."""

    def __init__(self, hass: HomeAssistant, email: str, password: str) -> None:
        """Set up a client with its own cookie jar and connection pool.

        create_async_httpx_client builds the SSL context in the executor, so
        this is safe to call from the event loop, and registers the client to
        be closed when Home Assistant stops.
        """
        self._email = email
        self._password = password
        self._client = create_async_httpx_client(
            hass,
            timeout=httpx.Timeout(REQUEST_TIMEOUT),
        )

        self._access_token: str | None = None
        self._token_type: str = "Bearer"
        self._refresh_token: str | None = None
        self._expires_at: float = 0.0

    async def async_close(self) -> None:
        """Close the underlying HTTP client.

        Called when the config entry is unloaded. Without it every reload —
        including the automatic one after saving options or completing a
        reauth — leaks keep-alive sockets for the life of the process.
        """
        await self._client.aclose()

    # --- Public API ---------------------------------------------------------

    async def async_validate_credentials(self) -> None:
        """Log in once, to check the credentials during config flow.

        Raises BrunataAuthError if they are wrong, BrunataConnectionError if
        Brunata could not be reached.
        """
        await self._async_login(force=True)

    async def async_get_meters(self) -> dict[str, BrunataMeter]:
        """Return every active meter, keyed by meter ID.

        Meters are fetched without a start date: with one, Brunata returns the
        first measurement of the period rather than the most recent.
        """
        response = await self._async_get_meters_once(force_login=False)

        # A cached token can look locally valid while the server no longer
        # accepts it — the Keycloak session may have been revoked, or our
        # clock may disagree with theirs. Rather than immediately declare the
        # credentials wrong and prompt the user, discard the token and try
        # exactly one brand-new login. Only a 401 on *that* attempt means the
        # credentials themselves are no longer accepted.
        if response.status_code in (401, 403):
            _LOGGER.warning(
                "Brunata returned %s with a cached token — retrying once with "
                "a fresh login",
                response.status_code,
            )
            response = await self._async_get_meters_once(force_login=True)

            if response.status_code in (401, 403):
                raise BrunataAuthError(
                    f"Brunata returned {response.status_code} even after a "
                    "fresh login. Check credentials and account access."
                )

        return _parse_meters(_payload(response))

    # --- HTTP ---------------------------------------------------------------

    async def _async_get_meters_once(self, *, force_login: bool) -> httpx.Response:
        await self._async_login(force=force_login)
        return await self._async_request(
            "GET",
            f"{API_URL}/consumer/meters",
            headers={
                "Authorization": f"{self._token_type} {self._access_token}",
                "Referer": METERS_URL,
            },
        )

    async def _async_request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Perform a request, mapping transport failures to our own error."""
        try:
            return await self._client.request(method, url, **kwargs)
        except httpx.TransportError as err:
            raise BrunataConnectionError(f"Cannot reach Brunata: {err}") from err

    # --- Authentication -----------------------------------------------------

    @property
    def _token_is_usable(self) -> bool:
        return bool(self._access_token) and time.time() < self._expires_at

    def _store_tokens(self, payload: dict[str, Any]) -> None:
        """Record a token response.

        Every field is replaced rather than merged. Merging previously let an
        old expiry survive a response that carried none, so an expired token
        could be reported as usable and the login it needed was skipped.
        """
        access_token = payload.get("access_token")
        if not access_token:
            raise BrunataApiError("Brunata returned no access token")

        self._access_token = access_token
        self._token_type = payload.get("token_type") or "Bearer"
        self._refresh_token = payload.get("refresh_token")

        expires_in = payload.get("expires_in")
        self._expires_at = (
            time.time() + float(expires_in) - _EXPIRY_MARGIN_SECONDS
            if expires_in is not None
            else 0.0
        )

    def _clear_tokens(self) -> None:
        self._access_token = None
        self._refresh_token = None
        self._expires_at = 0.0

    async def _async_login(self, *, force: bool = False) -> None:
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

        # When a Keycloak SSO session is still alive, this request is
        # redirected straight to the redirect URI carrying ?code=... and no
        # login form is rendered. That is success, not failure — treating the
        # missing form as an error would prompt the user to re-enter
        # credentials that are perfectly valid.
        if auth_code := _code_from_url(page.url):
            _LOGGER.debug("Brunata SSO session still active — no login form needed")
            return auth_code

        match = _KC_FORM_ACTION_RE.search(page.text)
        if not match:
            raise BrunataAuthError(
                "Brunata login form not found — the login flow has changed"
            )

        auth = await self._async_request(
            "POST",
            html.unescape(match.group(1)),
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

        location = auth.headers.get("Location", "")
        if not location.startswith(KC_REDIRECT_URI):
            raise BrunataAuthError(f"Unexpected redirect after login: {location}")

        auth_code = _code_from_url(location)
        if not auth_code:
            raise BrunataAuthError("Brunata returned no authorization code")
        return auth_code


def _code_from_url(url: Any) -> str | None:
    """Pull the OAuth authorization code out of a redirect URL."""
    return parse_qs(urlparse(str(url)).query).get("code", [None])[0]


def _payload(response: httpx.Response) -> Any:
    """Decode a JSON response, raising on anything that isn't usable."""
    status = response.status_code

    if status == 429:
        raise BrunataApiError("Brunata rate limit reached", status)
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


def _parse_meters(payload: Any) -> dict[str, BrunataMeter]:
    """Turn a /consumer/meters payload into meters keyed by ID."""
    if not isinstance(payload, list):
        raise BrunataApiError(
            f"Expected a list of meters, got {type(payload).__name__}"
        )

    meters: dict[str, BrunataMeter] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue

        raw_meter = item.get("meter")
        if not isinstance(raw_meter, dict):
            continue

        # Meters without a superAllocationUnit are typically inactive or
        # internal devices.
        if raw_meter.get("superAllocationUnit") is None:
            continue

        raw_id = raw_meter.get("meterId")
        if raw_id is None:
            continue

        reading = item.get("reading")
        value = reading.get("value") if isinstance(reading, dict) else None

        meters[str(raw_id)] = BrunataMeter(
            meter_id=str(raw_id),
            meter_no=raw_meter.get("meterNo"),
            meter_type=raw_meter.get("meterType") or "",
            unit=raw_meter.get("meterUnit") or "",
            value=float(value) if value is not None else None,
            reading_date=(
                _parse_reading_date(reading.get("readingDate"))
                if isinstance(reading, dict)
                else None
            ),
        )

    _LOGGER.debug("Parsed %s meters from Brunata", len(meters))
    return meters

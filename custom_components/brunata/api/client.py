"""The client that talks to Brunata Online on behalf of one account.

This is the only module in the package that makes requests, and the only one
that holds state: the HTTP client, the Keycloak tokens, the lookup tables and
what the last parse saw. Everything it does with what comes back belongs to
one of the modules it imports from.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
from datetime import datetime
from functools import partial
from typing import Any

import httpx

from homeassistant.core import HomeAssistant

from .auth import (
    _EXPIRY_MARGIN_SECONDS,
    _KC_FORM_ACTION_RE,
    _REDIRECT_STATUSES,
    _checked_form_action,
    _code_from_url,
    _expires_in_seconds,
)
from .const import (
    API_URL,
    DEFAULT_HEADERS,
    KC_AUTHORIZE_URL,
    KC_CLIENT_ID,
    KC_REDIRECT_URI,
    KC_SCOPE,
    KC_TOKEN_URL,
    LOCALE,
    REFERER_URL,
    REQUEST_TIMEOUT,
)
from .errors import BrunataApiError, BrunataAuthError, BrunataConnectionError
from .http import _payload
from .models import BrunataMeter, ParseReport
from .parse import _parse_daily_consumption, _parse_meters

_LOGGER = logging.getLogger(__name__)

class BrunataApiClient:
    """Talks to Brunata Online on behalf of one account."""

    def __init__(self, email: str, password: str, http_client: httpx.AsyncClient) -> None:
        """Use async_create() instead; it builds the HTTP client correctly."""
        self._email = email
        self._password = password
        self._client = http_client

        self._meter_types: list[str] = []
        self._measurement_units: list[str] = []
        # Whether the locale resource has been fetched, which is not the same
        # as the tables being non-empty. See _async_ensure_lookup_tables().
        self._lookup_tables_loaded = False

        # What the most recent parse saw. See last_parse_report().
        self._last_parse_report = ParseReport(frozenset(), 0)

        self._access_token: str | None = None
        self._token_type: str = "Bearer"
        self._refresh_token: str | None = None
        self._expires_at: float = 0.0

    @classmethod
    async def async_create(
        cls, hass: HomeAssistant, email: str, password: str
    ) -> BrunataApiClient:
        """Build a client with its own cookie jar and connection pool.

        Deliberately not homeassistant.helpers.httpx_client: that returns a
        client Home Assistant owns and closes at shutdown, and it warns when an
        integration closes it. We hold Keycloak session cookies and a bearer
        token and want them gone the moment the entry unloads, so we own it.

        Built in the executor because httpx.AsyncClient loads the certificate
        store from disk when it builds its SSL context.
        """
        http_client = await hass.async_add_executor_job(
            partial(
                httpx.AsyncClient,
                timeout=httpx.Timeout(REQUEST_TIMEOUT),
                headers=DEFAULT_HEADERS,
            )
        )
        return cls(email, password, http_client)

    def diagnostics(self) -> dict[str, Any]:
        """Return the client's internal state for the diagnostics download.

        A method rather than five attribute reads from diagnostics.py: what is
        safe to publish belongs with the fields. Vendoring this client removed
        a dozen reads of a third party's private attributes, and there is no
        reason to reintroduce the pattern against ourselves.

        Neither token is included, only whether one exists. An access token is
        a working credential, and diagnostics files get attached to public
        issues.
        """
        return {
            "lookup_tables_loaded": self._lookup_tables_loaded,
            # Copied, not handed out: the caller serialises these into a file
            # and must not be able to mutate what the parser looks up in.
            "meter_types": list(self._meter_types),
            "measurement_units": list(self._measurement_units),
            "has_access_token": self._access_token is not None,
            "has_refresh_token": self._refresh_token is not None,
        }

    def last_parse_report(self) -> ParseReport:
        """Return what the most recent async_get_meters() call saw.

        A method rather than a bare attribute for the same reason as
        diagnostics(): what the rest of the integration may read off this
        client is decided here, next to the field.

        The coordinator is the only caller and its updates are serialised, so
        there is nothing to race. Callers must read it immediately after the
        call it belongs to.

        See ParseReport for why the meters dictionary alone is not enough.
        """
        return self._last_parse_report

    async def async_close(self) -> None:
        """Close the underlying HTTP client.

        Called on unload. Without it every reload — including the automatic one
        after a reauth — leaks keep-alive sockets for the life of the process.
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
        """Return every mounted meter, keyed by meter ID.

        One call to /consumer/metersforconsumer, which is what Brunata's own
        readings page uses: meter, latest reading, placement, mounting and
        dismounting dates and display precision in a single flat list.
        """
        await self._async_ensure_lookup_tables()

        response = await self._async_authenticated_get(
            f"{API_URL}/consumer/metersforconsumer"
        )

        parsed = _parse_meters(
            _payload(response),
            meter_types=self._meter_types,
            measurement_units=self._measurement_units,
        )
        self._last_parse_report = parsed.report
        return parsed.meters

    async def async_get_daily_consumption(
        self,
        meter_id: str,
        allocation_unit: str,
        start: datetime,
        end: datetime,
    ) -> list[float | None] | None:
        """Return Brunata's own consumption for one meter, one entry per day.

        Only the days that begin at or after `start` and before `end` are
        returned, in Brunata's order. An entry is None where Brunata has no
        figure for that day; it sends null for days it could not compute, and
        that is not the same as zero. The whole result is None when Brunata's
        answer does not contain the meter at all.

        This is how sensor.py asks whether a fall in a heat cost allocator's
        reading was the annual reset. Brunata zeroes allocators on a date set
        by the building's billing period, which is not the same everywhere
        and is not in any payload. Brunata's own consumption figures, however,
        are computed across that reset: on the maintainer's account the day of
        the 1 January 2026 reset shows ordinary positive consumption, not a
        fall the size of the year's total.

        The endpoint is the one Brunata's consumption page calls, read off the
        browser's network inspector. It takes no meter id. It returns every
        meter in one allocationUnit group, so the meter is picked out of the
        answer by meterId.

        The timestamps are sent the way the page sends them: local time with
        milliseconds and an offset, e.g. 2026-09-11T00:00:00.000+02:00.
        """
        response = await self._async_authenticated_get(
            f"{API_URL}/consumer/consumption",
            params={
                "startdate": start.isoformat(timespec="milliseconds"),
                "enddate": end.isoformat(timespec="milliseconds"),
                "interval": "D",
                "allocationunit": allocation_unit,
            },
        )
        return _parse_daily_consumption(_payload(response), meter_id, start, end)

    async def _async_ensure_lookup_tables(self) -> None:
        """Fetch the locale resource once per client, or fail the update.

        The meter payload identifies type and unit by index into these tables.
        Without them every meter is united by a bare number — "8" where the
        user had "m³" — and Home Assistant treats a changed unit as a new
        series, discarding the statistics behind the old one. That cannot be
        undone.

        So an unusable response is an error, not a warning: failing costs one
        poll, and the sensors keep their values until the next hour. An empty
        table counts as unusable too — both mean no lookups are possible, and
        the shape of the failure is Brunata's business.

        The caching guard is a separate flag rather than "are the tables
        non-empty", and it is only set on the success path. Its job is to stop
        a *loaded* table being re-fetched; leaving it unset when nothing loaded
        is a retry, not that loop.
        """
        if self._lookup_tables_loaded:
            return

        response = await self._async_authenticated_get(
            f"{API_URL}/locales/{LOCALE}/common"
        )
        payload = _payload(response)
        # Guarded rather than assumed: a payload that is a list (or anything
        # else) would otherwise raise AttributeError here instead of the
        # BrunataApiError the coordinator knows how to translate.
        mappers = payload.get("mappers") if isinstance(payload, dict) else None
        if not isinstance(mappers, dict):
            raise BrunataApiError("Brunata locale resource carried no mappers")

        meter_types = list(mappers.get("meterType") or [])
        measurement_units = list(mappers.get("measurementUnit") or [])

        if not meter_types or not measurement_units:
            raise BrunataApiError(
                f"Brunata locale resource carried {len(meter_types)} meter "
                f"types and {len(measurement_units)} units, so meter types "
                "and units cannot be resolved"
            )

        self._meter_types = meter_types
        self._measurement_units = measurement_units
        self._lookup_tables_loaded = True

        # Logged in full, not just counted. These are Brunata's own translation
        # tables — static, identical for every account, free of personal data —
        # and the only authoritative answer to which meter types and units the
        # service can express at all. Any one account uses a handful.
        _LOGGER.debug(
            "Loaded Brunata lookup tables (%s meter types, %s units). "
            "meterType=%s measurementUnit=%s",
            len(self._meter_types),
            len(self._measurement_units),
            self._meter_types,
            self._measurement_units,
        )

    # --- HTTP ---------------------------------------------------------------

    async def _async_authenticated_get(
        self, url: str, params: dict[str, str] | None = None
    ) -> httpx.Response:
        """GET an API endpoint, retrying once with a brand-new login on 401/403.

        A cached token can look locally valid while the server no longer
        accepts it — a revoked Keycloak session, or clock drift. Rather than
        declaring the credentials wrong, discard the token and try exactly one
        fresh login. Only a 401 on *that* attempt means the credentials are no
        longer accepted.

        Shared by every endpoint on purpose. Confined to the meters call, a
        stale token on the locale resource would surface as "invalid JSON" —
        and since the tables are fetched once per client, that would repeat on
        every poll until something else happened to fix it.
        """
        response = await self._async_get(url, force_login=False, params=params)
        if response.status_code not in (401, 403):
            return response

        _LOGGER.warning(
            "Brunata returned %s with a cached token — retrying once with a "
            "fresh login",
            response.status_code,
        )
        response = await self._async_get(url, force_login=True, params=params)

        if response.status_code in (401, 403):
            raise BrunataAuthError(
                f"Brunata returned {response.status_code} even after a fresh "
                "login. Check credentials and account access."
            )
        return response

    async def _async_get(
        self,
        url: str,
        *,
        force_login: bool,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Log in if needed, then GET the URL with the API's expected headers."""
        await self._async_login(force=force_login)
        return await self._async_request(
            "GET",
            url,
            params=params,
            headers={
                "Authorization": f"{self._token_type} {self._access_token}",
                "Referer": REFERER_URL,
            },
        )

    async def _async_request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
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
            return await self._client.request(method, url, **kwargs)
        except httpx.RequestError as err:
            raise BrunataConnectionError(f"Cannot reach Brunata: {err}") from err

    # --- Authentication -----------------------------------------------------

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

"""Client for the Brunata Online API.

This replaces the external ``brunata-api`` package. That package targeted
Brunata's retired Azure AD B2C login and v1 data API, so the integration had to
monkey-patch ``Client._get_tokens`` and rebind the module-level ``API_URL`` at
import time, then reach into ``_session``, ``_username``, ``_password``,
``_tokens``, ``_is_token_valid``, ``_meters`` and ``_init_mappers``. Every one
of those is private, so any release of the library could have broken the
integration at runtime with no warning.

Everything the integration actually used is implemented here instead: the
Keycloak login and the single ``/consumer/metersforconsumer`` call that
Brunata's own readings page uses
label. The HTTP client comes from Home Assistant, which builds the SSL context
off the event loop and closes the client at shutdown.
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
from datetime import date, datetime
from functools import partial
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Brunata retired the v1 data API alongside the Keycloak migration: with a
# valid token, the v1 equivalent of this endpoint answers 401 while v2
# returns the meter data.
BASE_URL = "https://online.brunata.com"
API_URL = f"{BASE_URL}/online-webservice/v2/rest"

# Sent as the Referer on API calls. Taken from brunata-api 0.1.6, which is what
# the endpoints were verified against.
METERS_URL = f"{BASE_URL}/react-online/meters-values"

# Brunata's API is fronted by bot protection, so the requests are made to look
# like the web app's. brunata-api randomised the Edge version through
# fake_useragent; a fixed, plausible string avoids that dependency.
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0"
    ),
    "Sec-Ch-Ua": (
        '"Not/A)Brand";v="8", "Chromium";v="130", "Microsoft Edge";v="130"'
    ),
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Accept-Language": "en",
}

# The locale resource carries the lookup tables that turn the numeric codes in
# the meter payload into names and units.
LOCALE = "en"

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

    Flat by design: every field comes straight from one entry in the
    /consumer/metersforconsumer payload, so an absent reading is simply
    ``value is None`` rather than a missing nested object.
    """

    meter_id: str
    meter_no: str | None
    meter_type: str
    unit: str
    value: float | None = None
    reading_date: date | None = None
    # The customer-assigned label from Brunata's own UI, e.g. "Koldt vand" or
    # "Soveværelse". None when the meter has no label set.
    placement: str | None = None
    # When Brunata installed the physical device. A change here is a meter
    # replacement stated as fact, rather than inferred from a falling value.
    mounting_date: datetime | None = None
    # Digits Brunata itself displays: 3 for water, 0 for heat cost allocators.
    decimals: int | None = None
    # Whether the meter is currently sending readings.
    transmitting: bool | None = None


def _as_text(raw: Any) -> str:
    """Coerce a lookup code to text.

    Brunata's v2 payload carries meter type and unit as numeric codes, which
    _lookup() resolves against the tables from the locale resource. Those codes
    have been observed as both integers and strings, so they are normalised
    here before being parsed, and an absent code becomes "" rather than the
    string "None".
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    return str(raw)


def _lookup(table: list[str], raw: Any, what: str) -> str:
    """Resolve a numeric code against one of the locale lookup tables.

    The meter payload carries indices, not names: meterType 2 means whatever
    sits at index 2 of the meterType table. An unresolved code falls back to
    the raw number so the entity is still created, rather than taking down the
    whole platform.
    """
    code = _as_text(raw)
    if not code:
        return ""

    try:
        name = table[int(code)]
    except (ValueError, IndexError):
        name = None

    # The live tables contain null entries — 7 of 28 meter types and 34 of 96
    # units are None, reserved slots Brunata has not filled in. An index
    # landing on one must be treated exactly like an index past the end:
    # returning the None would put it straight into BrunataMeter.meter_type,
    # and the sensor platform would then die on meter_type.lower(), taking
    # every entity with it rather than just the odd one.
    if not isinstance(name, str) or not name.strip():
        _LOGGER.warning(
            "Brunata %s code %r does not resolve in the lookup table "
            "(%s entries). Falling back to the raw code.",
            what,
            code,
            len(table),
        )
        return code

    # Some entries carry a trailing space ("Electricity ", "Carbon dioxide "),
    # which would otherwise end up in the device name.
    return name.strip()


def _parse_timestamp(raw: Any) -> datetime | None:
    """Parse one of Brunata's ISO timestamps, e.g. mountingDate.

    They carry an offset ("2018-10-23T14:09:22+02:00"), so the result is
    timezone-aware. Anything unparseable becomes None rather than raising: a
    meter with an odd date is still a meter.
    """
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            _LOGGER.debug("Could not parse timestamp %r", raw)
    return None


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
        integration closes it. Since we hold Keycloak session cookies and a
        bearer token, and want them gone the moment the config entry unloads,
        we own the client ourselves.

        httpx.AsyncClient loads the certificate store from disk when it builds
        its SSL context, so it is constructed in the executor rather than on
        the event loop.
        """
        http_client = await hass.async_add_executor_job(
            partial(
                httpx.AsyncClient,
                timeout=httpx.Timeout(REQUEST_TIMEOUT),
                headers=DEFAULT_HEADERS,
            )
        )
        return cls(email, password, http_client)

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
        """Return every mounted meter, keyed by meter ID.

        One call to /consumer/metersforconsumer, which is what Brunata's own
        readings page uses. It carries the meter, its latest reading, its
        customer-assigned placement, its mounting and dismounting dates and its
        display precision in a single flat list.
        """
        await self._async_ensure_lookup_tables()

        response = await self._async_fetch_meters(force_login=False)

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
            response = await self._async_fetch_meters(force_login=True)

            if response.status_code in (401, 403):
                raise BrunataAuthError(
                    f"Brunata returned {response.status_code} even after a "
                    "fresh login. Check credentials and account access."
                )

        return _parse_meters(
            _payload(response),
            meter_types=self._meter_types,
            measurement_units=self._measurement_units,
        )

    async def _async_ensure_lookup_tables(self) -> None:
        """Fetch the locale resource once per client.

        The meter payload identifies type and unit by index into these tables,
        so without them a water meter's unit is unknown — which Home Assistant
        treats as a unit change and responds to by suppressing the sensor's
        long term statistics.

        The guard is a separate flag rather than "are the tables non-empty".
        A response carrying a mappers object with an empty or missing
        meterType would leave the list empty, so a non-empty check would never
        be satisfied and this endpoint would be re-fetched on every single
        update, forever, for no benefit.
        """
        if self._lookup_tables_loaded:
            return

        await self._async_login()
        response = await self._async_request(
            "GET",
            f"{API_URL}/locales/{LOCALE}/common",
            headers={
                "Authorization": f"{self._token_type} {self._access_token}",
                "Referer": METERS_URL,
            },
        )
        payload = _payload(response)
        # Guarded rather than assumed: a payload that is a list (or anything
        # else) would otherwise raise AttributeError here instead of the
        # BrunataApiError the coordinator knows how to translate.
        mappers = payload.get("mappers") if isinstance(payload, dict) else None
        if not isinstance(mappers, dict):
            raise BrunataApiError("Brunata locale resource carried no mappers")

        self._meter_types = list(mappers.get("meterType") or [])
        self._measurement_units = list(mappers.get("measurementUnit") or [])
        self._lookup_tables_loaded = True

        if not self._meter_types or not self._measurement_units:
            # Not fatal: _lookup() falls back to the raw code, so entities are
            # still created. But every meter will be named and united by a bare
            # number, so say so once rather than only per meter.
            _LOGGER.warning(
                "Brunata locale resource carried %s meter types and %s units. "
                "Meter types and units will fall back to their raw codes.",
                len(self._meter_types),
                len(self._measurement_units),
            )
        else:
            # Logged in full, not just counted. These are Brunata's own
            # translation tables — static, identical for every account and free
            # of personal data — and they are the only authoritative answer to
            # which meter types and units the service can express at all. The
            # meters on any one account use a handful of the entries.
            _LOGGER.debug(
                "Loaded Brunata lookup tables (%s meter types, %s units). "
                "meterType=%s measurementUnit=%s",
                len(self._meter_types),
                len(self._measurement_units),
                self._meter_types,
                self._measurement_units,
            )

    # --- HTTP ---------------------------------------------------------------

    async def _async_fetch_meters(self, *, force_login: bool) -> httpx.Response:
        await self._async_login(force=force_login)
        return await self._async_request(
            "GET",
            f"{API_URL}/consumer/metersforconsumer",
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


def _parse_meters(
    payload: Any,
    *,
    meter_types: list[str] | None = None,
    measurement_units: list[str] | None = None,
) -> dict[str, BrunataMeter]:
    """Turn a /consumer/metersforconsumer payload into meters keyed by ID.

    Each entry is flat — meter, latest reading and metadata side by side — so
    there is no wrapper object to guard against.
    """
    if not isinstance(payload, list):
        raise BrunataApiError(
            f"Expected a list of meters, got {type(payload).__name__}"
        )

    _LOGGER.debug("Brunata returned %s raw item(s)", len(payload))

    meters: dict[str, BrunataMeter] = {}
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            _LOGGER.debug(
                "Item %s skipped: not an object (%s)", index, type(item).__name__
            )
            continue

        raw_id = item.get("meterId")
        if raw_id is None:
            _LOGGER.debug("Item %s skipped: meterId is null", index)
            continue

        # A dismounted meter is one Brunata has physically removed. Its final
        # reading never changes again, so carrying it would leave a device in
        # Home Assistant frozen forever. Dropping it here makes the entity go
        # unavailable instead, which is what a removed meter should look like.
        dismounted = _parse_timestamp(item.get("dismountedDate"))
        if dismounted is not None:
            _LOGGER.debug(
                "Item %s (meter %s) skipped: dismounted on %s",
                index,
                raw_id,
                dismounted.date(),
            )
            continue

        value = item.get("latestReadingValue")
        decimals = item.get("decimals")
        placement = item.get("placement")
        transmitting = item.get("transmitting")

        _LOGGER.debug(
            "Item %s: meterId=%r meterType=%r unit=%r decimals=%r "
            "transmitting=%r has reading=%s",
            index,
            raw_id,
            item.get("meterType"),
            item.get("unit"),
            decimals,
            transmitting,
            value is not None,
        )

        meters[str(raw_id)] = BrunataMeter(
            meter_id=str(raw_id),
            meter_no=item.get("meterNo"),
            meter_type=_lookup(meter_types or [], item.get("meterType"), "meter type"),
            unit=_lookup(
                measurement_units or [], item.get("unit"), "measurement unit"
            ),
            value=float(value) if value is not None else None,
            reading_date=_parse_reading_date(item.get("latestReadingDate")),
            placement=placement if isinstance(placement, str) and placement else None,
            mounting_date=_parse_timestamp(item.get("mountingDate")),
            decimals=int(decimals) if isinstance(decimals, int) else None,
            transmitting=transmitting if isinstance(transmitting, bool) else None,
        )

    _LOGGER.debug("Parsed %s meters from Brunata", len(meters))
    return meters

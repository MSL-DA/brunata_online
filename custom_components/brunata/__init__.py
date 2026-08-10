"""The Brunata integration."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import logging
import os
import re
import socket
from datetime import datetime
from urllib.parse import parse_qs, urlparse

try:
    # httpx is a dependency of brunata_api and always available at runtime.
    # The IDE may not resolve it if httpx is not installed in the dev environment.
    import httpx as _httpx
    _CONNECT_ERRORS = (ConnectionError, UnboundLocalError, _httpx.ConnectError, _httpx.ConnectTimeout, _httpx.ReadTimeout)
except ImportError:
    _httpx = None
    _CONNECT_ERRORS = (ConnectionError, UnboundLocalError)

import brunata_api as _brunata_api
from brunata_api import Client
from brunata_api.const import OAUTH2_URL, CLIENT_ID

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, CONF_EMAIL, CONF_PASSWORD, CONF_DEBUG_LOGGING

_LOGGER = logging.getLogger(__name__)


async def _check_connectivity(host: str, port: int = 443, timeout: float = 5.0) -> bool:
    """Quick TCP check to verify network reachability before invoking the library."""
    try:
        loop = asyncio.get_event_loop()
        await asyncio.wait_for(
            loop.run_in_executor(None, socket.create_connection, (host, port)),
            timeout=timeout,
        )
        return True
    except Exception:
        return False


# Monkeypatch to fix bug in brunata_api library where it awaits a dict in _renew_tokens
async def _renew_tokens_fixed(self) -> dict:
    """Renew access token using refresh token."""
    if self._is_token_valid("access_token"):
        _LOGGER.debug(
            "Token is not expired, expires in %d seconds",
            self._tokens.get("expires_on") - int(datetime.now().timestamp()),
        )
        return self._tokens
    # Get OAuth 2.0 token object
    try:
        tokens = await self.api_wrapper(
            method="POST",
            url=f"{OAUTH2_URL}/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": self._tokens.get("refresh_token"),
                "CLIENT_ID": CLIENT_ID,
            },
        )
    except UnboundLocalError:
        # Library bug: api_wrapper catches ConnectError but then tries to return an
        # unassigned 'response' variable — treat this as a network connectivity error.
        raise ConnectionError("Brunata token server unreachable") from None
    except Exception:
        _LOGGER.exception("An error occurred while trying to renew tokens")
        raise
    return tokens.json()

Client._renew_tokens = _renew_tokens_fixed


# --- Keycloak login override -------------------------------------------------
# In mid-2026 Brunata migrated authentication from Azure AD B2C
# (brunatab2cprod.b2clogin.com) to Keycloak (realm "online-prod"). The
# brunata_api library still requests the old Azure OAuth client, so Keycloak
# rejects login with HTTP 400 "clientNotFoundMessage", which then cascades to a
# 401 on every data call. The values below are taken from Brunata's current web
# app and verified against the live Keycloak server.
KC_REALM_BASE = "https://online.brunata.com/iam/realms/online-prod/protocol/openid-connect"
KC_AUTHORIZE_URL = f"{KC_REALM_BASE}/auth"
KC_TOKEN_URL = f"{KC_REALM_BASE}/token"
KC_CLIENT_ID = "82770188-c92e-4d16-927d-a15c472eda55"
KC_REDIRECT_URI = "https://online.brunata.com/auth-redirect"
KC_SCOPE = "openid offline_access"
# Keycloak login form is served as <form id="kc-form-login" ... action="...">
_KC_FORM_ACTION_RE = re.compile(r'id="kc-form-login"[^>]*action="([^"]+)"', re.IGNORECASE)
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)


async def _keycloak_get_tokens(self) -> bool:
    """Authenticate against Brunata's Keycloak realm using the OAuth 2.0
    Authorization Code + PKCE flow, and store the access token on the session.

    This fully replaces the library's stale Azure-B2C login. It only relies on
    the client's ``_session`` (an ``httpx.AsyncClient``), ``_username`` and
    ``_password`` attributes, so it is independent of the library's internals.
    Raises ``ConfigEntryAuthFailed`` on bad credentials so HA can prompt for
    re-authentication.
    """
    session = self._session

    # Reuse a still-valid token. The library calls _get_tokens() several times
    # per update cycle (directly, then via _init_mappers/get_meters); without
    # this we would perform a full browser login on every call.
    try:
        if self._is_token_valid("access_token") and session.headers.get("Authorization"):
            return True
    except Exception:  # noqa: BLE001 — be defensive about library internals
        pass

    # Drop any stale Authorization header before logging in again.
    session.headers.pop("Authorization", None)

    # PKCE challenge
    code_verifier = re.sub(
        "[^a-zA-Z0-9]+", "", base64.urlsafe_b64encode(os.urandom(40)).decode("utf-8")
    )
    code_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode("utf-8")).digest())
        .decode("utf-8")
        .rstrip("=")
    )

    # 1) Fetch the login page (sets AUTH_SESSION_ID / KC_RESTART cookies).
    page = await session.get(
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
    page.raise_for_status()

    match = _KC_FORM_ACTION_RE.search(page.text)
    if not match:
        _LOGGER.error("Brunata Keycloak login form not found — auth flow may have changed")
        raise ConfigEntryAuthFailed("Brunata login form not found (Keycloak flow changed)")
    form_action = html.unescape(match.group(1))

    # 2) Submit credentials. On success Keycloak issues a 302 to the redirect
    #    URI carrying the authorization code; on failure it re-renders the form.
    auth = await session.post(
        form_action,
        data={
            "username": self._username,
            "password": self._password,
            "credentialId": "",
        },
        follow_redirects=False,
    )
    if auth.status_code not in _REDIRECT_STATUSES:
        _LOGGER.error("Brunata authentication failed (status %s) — check credentials", auth.status_code)
        raise ConfigEntryAuthFailed("Brunata authentication failed — check email and password")

    location = auth.headers.get("Location", "")
    if not location.startswith(KC_REDIRECT_URI):
        _LOGGER.error("Unexpected redirect after Brunata login: %s", location)
        raise ConfigEntryAuthFailed("Unexpected redirect after Brunata login")

    auth_code = parse_qs(urlparse(location).query).get("code", [None])[0]
    if not auth_code:
        _LOGGER.error("No authorization code returned by Brunata login")
        raise ConfigEntryAuthFailed("No authorization code returned by Brunata")

    # 3) Exchange the code for tokens (public client + PKCE, no secret needed).
    token_resp = await session.post(
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
    token_resp.raise_for_status()
    tokens = token_resp.json()

    if not tokens.get("access_token"):
        self._tokens = {}
        _LOGGER.error("Brunata token endpoint returned no access_token")
        raise ConfigEntryAuthFailed("Brunata did not return an access token")

    # Normalise expiry fields to what the library's helpers expect, then store.
    now = int(datetime.now().timestamp())
    if tokens.get("expires_in") is not None:
        tokens["expires_on"] = now + int(tokens["expires_in"])
    if tokens.get("refresh_expires_in") is not None:
        tokens["refresh_token_expires_on"] = now + int(tokens["refresh_expires_in"])

    session.headers.update(
        {"Authorization": f"{tokens.get('token_type', 'Bearer')} {tokens['access_token']}"}
    )
    self._tokens.update(tokens)
    return True


Client._get_tokens = _keycloak_get_tokens


# --- Data API moved to v2 ----------------------------------------------------
# Alongside the Keycloak migration, Brunata retired the v1 data API: with a
# valid token, /online-webservice/v1/rest/consumer/meters returns 401
# "Not authorized" while the v2 endpoint returns the meter data. The brunata_api
# library still points API_URL at v1, so we redirect it (and our own calls
# below) to v2. The library resolves API_URL as a module global at call time,
# so patching it here fixes get_meters()/update_meters()/_init_mappers() too.
API_URL_V2 = "https://online.brunata.com/online-webservice/v2/rest"
_brunata_api.API_URL = API_URL_V2
# -----------------------------------------------------------------------------

PLATFORMS: list[str] = ["sensor"]

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Brunata from a config entry."""
    if entry.options.get(CONF_DEBUG_LOGGING):
        _LOGGER.setLevel(logging.DEBUG)
        logging.getLogger("brunata_api").setLevel(logging.DEBUG)
        _LOGGER.debug("Debug logging enabled via settings")

    email = entry.data[CONF_EMAIL]
    password = entry.data[CONF_PASSWORD]

    if not await _check_connectivity("online.brunata.com"):
        raise ConfigEntryNotReady("Cannot reach Brunata servers — network not ready, will retry")

    _LOGGER.debug("Setting up Brunata integration for %s", email)
    client = await hass.async_add_executor_job(Client, email, password)

    # Brunata's servers can respond slowly on the /consumer/meters endpoint.
    # httpx's default timeout is 5 seconds for connect/read/write/pool each,
    # which is too aggressive here and causes spurious ReadTimeout errors.
    # Only applied if httpx was successfully imported above.
    if _httpx is not None:
        client._session.timeout = _httpx.Timeout(15.0)

    coordinator = BrunataDataUpdateCoordinator(hass, client)

    # Initial data refresh
    _LOGGER.debug("Performing initial data refresh")
    try:
        await coordinator.async_config_entry_first_refresh()
    except UpdateFailed as err:
        if "authentication failed" in str(err).lower():
            raise ConfigEntryAuthFailed from err
        raise

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    @callback
    def _handle_scheduled_refresh(now) -> None:
        """Trigger a coordinator refresh.

        Must be decorated with @callback so Home Assistant knows this
        function is safe to run directly on the event loop. Without it, HA
        assumes the callback is blocking and dispatches it to a worker
        thread instead — and hass.async_create_task() may only be called
        from the event loop, which caused a thread-safety RuntimeError.
        """
        hass.async_create_task(coordinator.async_request_refresh())

    # Poll 2 minutes before every new hour (xx:59:30), instead of relying on
    # DataUpdateCoordinator's rolling update_interval, which drifts based on
    # whenever HA last started or the integration was last reloaded.
    entry.async_on_unload(
        async_track_time_change(
            hass,
            _handle_scheduled_refresh,
            minute=59,
            second=30,
        )
    )

    _LOGGER.debug("Forwarding setups to platforms: %s", PLATFORMS)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    
    entry.async_on_unload(entry.add_update_listener(async_update_options))

    return True

async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Update options."""
    await hass.config_entries.async_reload(entry.entry_id)

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok

class BrunataDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching Brunata data."""

    def __init__(self, hass: HomeAssistant, client: Client):
        """Initialize."""
        self.client = client
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            # No update_interval: polling is instead driven by an
            # async_track_time_change listener (xx:59:30 every hour, see
            # async_setup_entry), so the schedule is fixed to wall-clock
            # time rather than drifting from whenever HA last started or
            # the integration was last reloaded.
        )

    async def _async_update_data(self):
        """Fetch data from API."""
        _LOGGER.debug("Starting data update from Brunata API")
        try:
            # We fetch meters without startdate to get the absolute latest measurements
            # for all meters. Brunata's API returns the first measurement in a period
            # if startdate is specified, which gives outdated data.
            
            # The library has a bug where it tries to await a dict
            # in _get_tokens -> _renew_tokens and _b2c_auth.
            # We wrap it in a try-except to provide a better error message
            # if it fails in the library.
            try:
                _LOGGER.debug("Refreshing tokens and initializing mappers")
                await self.client._get_tokens()
                await self.client._init_mappers()
            except TypeError as err:
                if "await" in str(err) and "dict" in str(err):
                    _LOGGER.error("Error in brunata-api library: 'object dict can't be used in await expression'. Ensure you have a fixed version of the library or contact the developer.")
                raise UpdateFailed(f"Error communicating with Brunata API via library: {err}") from err

            # Note: API_URL_V2 (not the library's stale v1 const) — the v1
            # data API now returns 401 for authenticated requests.
            from brunata_api.const import METERS_URL
            from brunata_api import Meter

            # Fetch all meters with their latest status
            _LOGGER.debug("Fetching meters from %s/consumer/meters", API_URL_V2)
            response = await self.client.api_wrapper(
                method="GET",
                url=f"{API_URL_V2}/consumer/meters",
                headers={
                    "Referer": METERS_URL,
                },
            )

            if response is None:
                _LOGGER.warning("No response from API (timeout or connection error)")
                return dict(self.client._meters)

            _LOGGER.debug("API response from /consumer/meters: %s", response.text)

            try:
                result = response.json()
            except Exception as json_err:
                _LOGGER.error("Error parsing JSON from API: %s. Response: %s", json_err, response.text)
                return dict(self.client._meters)

            if not isinstance(result, list):
                if isinstance(result, dict) and (
                    result.get("errorCode") is not None
                    or result.get("errorMessage") is not None
                ):
                    error_code = result.get("errorCode") or result.get("error_code")
                    error_message = result.get("errorMessage") or result.get("error_message")
                    _LOGGER.error(
                        "Brunata API returned error response: %s %s",
                        error_code,
                        error_message,
                    )
                    if error_code == "WB_WEBSERVICES_0011" or (
                        isinstance(error_message, str)
                        and "Not authorized" in error_message
                    ):
                        raise ConfigEntryAuthFailed(
                            "Brunata API authentication failed. Check credentials and account access."
                        )
                    raise UpdateFailed(
                        f"Brunata API returned error {error_code}: {error_message}"
                    )

                _LOGGER.error("Unexpected API response format: expected list, got %s. Response: %s", type(result), response.text)
                return dict(self.client._meters)

            # Clear existing meters so readings don't accumulate across updates
            self.client._meters.clear()

            _LOGGER.debug("Processing %s items from API", len(result))
            for item in result:
                if not isinstance(item, dict):
                    continue

                json_meter = item.get("meter")
                if not isinstance(json_meter, dict):
                    continue

                # Filter meters without superAllocationUnit (often inactive or internal devices)
                if json_meter.get("superAllocationUnit") is None:
                    _LOGGER.debug("Skipping meter %s as it has no superAllocationUnit", json_meter.get("meterId"))
                    continue

                json_reading = item.get("reading")
                meter_id = str(json_meter.get("meterId"))

                _LOGGER.debug("Processing meter %s: %s", meter_id, json_meter.get("meterNo"))

                meter = Meter(self.client, json_meter)
                self.client._meters[meter_id] = meter

                if isinstance(json_reading, dict) and json_reading.get("value") is not None:
                    _LOGGER.debug(
                        "Adding reading for %s: %s (date: %s). Raw data: %s",
                        meter_id,
                        json_reading.get("value"),
                        json_reading.get("readingDate"),
                        json_reading,
                    )
                    meter.add_reading(json_reading)

            if not self.client._meters:
                _LOGGER.warning("No meters found. Attempting default fetch via get_meters().")
                try:
                    meters = await self.client.get_meters()
                    if meters:
                        _LOGGER.debug("Found %s meters via get_meters()", len(meters))
                        # get_meters() populates self.client._meters; return a copy of it
                        return dict(self.client._meters)
                except Exception as get_meters_err:
                    _LOGGER.error("Error calling get_meters(): %s", get_meters_err)

            # Return a copy of the dictionary to ensure the coordinator detects changes
            _LOGGER.debug("Data update complete. Total meters: %s", len(self.client._meters))
            return dict(self.client._meters)
        except (UpdateFailed, ConfigEntryAuthFailed):
            # ConfigEntryAuthFailed (e.g. bad credentials during Keycloak login)
            # must propagate so HA starts the re-authentication flow.
            raise
        except _CONNECT_ERRORS as err:
            # Covers httpx.ConnectError/ConnectTimeout/ReadTimeout (network unavailable),
            # ConnectionError (raised by _renew_tokens_fixed), and UnboundLocalError
            # (library bug in api_wrapper when a ConnectError occurs mid-call).
            if self.data is not None:
                # Keep sensors available with their last known values instead of going unavailable.
                _LOGGER.info("Cannot connect to Brunata — keeping last known values")
                return self.data
            raise UpdateFailed("Cannot connect to Brunata — will retry next interval") from err
        except Exception as err:
            raise UpdateFailed(f"Unexpected error fetching data: {err}") from err

"""The client that fetches Brunata's data on behalf of one account.

It owns the HTTP client, the lookup tables and what the last parse saw. The
login is not here: BrunataAuth holds the credentials and the tokens, and this
class asks it for an Authorization header without ever reading a token
itself. Everything it does with what comes back belongs to one of the modules
it imports from.
"""

from __future__ import annotations

import logging
from datetime import datetime
from functools import partial
from typing import Any

import httpx

from homeassistant.core import HomeAssistant

from .auth import BrunataAuth
from .const import API_URL, DEFAULT_HEADERS, LOCALE, REFERER_URL, REQUEST_TIMEOUT
from .errors import BrunataApiError, BrunataAuthError
from .http import _payload, async_request
from .models import BrunataMeter, ParseReport
from .parse import _parse_daily_consumption, _parse_meters

_LOGGER = logging.getLogger(__name__)

class BrunataApiClient:
    """Talks to Brunata Online on behalf of one account."""

    def __init__(self, email: str, password: str, http_client: httpx.AsyncClient) -> None:
        """Use async_create() instead; it builds the HTTP client correctly."""
        self._client = http_client
        # Given the same HTTP client, not one of its own: Keycloak's SSO
        # cookies live on it, and a forced login clears them.
        self._auth = BrunataAuth(email, password, http_client)

        self._meter_types: list[str] = []
        self._measurement_units: list[str] = []
        # Whether the locale resource has been fetched, which is not the same
        # as the tables being non-empty. See _async_ensure_lookup_tables().
        self._lookup_tables_loaded = False

        # What the most recent parse saw. See last_parse_report().
        self._last_parse_report = ParseReport(frozenset(), 0)

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

        The two token keys come from BrunataAuth for the same reason: it owns
        the tokens, so it decides what may be said about them. Neither token
        itself is included — see BrunataAuth.diagnostics().
        """
        return {
            "lookup_tables_loaded": self._lookup_tables_loaded,
            # Copied, not handed out: the caller serialises these into a file
            # and must not be able to mutate what the parser looks up in.
            "meter_types": list(self._meter_types),
            "measurement_units": list(self._measurement_units),
            **self._auth.diagnostics(),
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
        await self._auth.async_ensure_token(force=True)

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
        authorization = await self._auth.async_authorization(force=force_login)
        return await self._async_request(
            "GET",
            url,
            params=params,
            headers={"Authorization": authorization, "Referer": REFERER_URL},
        )

    async def _async_request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Perform a request on this client's own HTTP client.

        The translation of a transport failure into BrunataConnectionError
        lives in http.async_request(), because BrunataAuth needs the same
        translation and must not reach into this class for it.
        """
        return await async_request(self._client, method, url, **kwargs)

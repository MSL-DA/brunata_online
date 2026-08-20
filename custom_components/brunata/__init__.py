"""The Brunata integration."""

from __future__ import annotations

import logging
from datetime import datetime

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    BrunataApiClient,
    BrunataApiError,
    BrunataAuthError,
    BrunataConnectionError,
    BrunataMeter,
)
from .const import CONF_DEBUG_LOGGING, CONF_EMAIL, CONF_PASSWORD, DOMAIN

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = ["sensor"]

type BrunataConfigEntry = ConfigEntry[BrunataDataUpdateCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: BrunataConfigEntry) -> bool:
    """Set up Brunata from a config entry."""
    # Set the level explicitly in both directions. Setting only the DEBUG case
    # would leave the loggers stuck at DEBUG until the next restart once the
    # option is turned back off. NOTSET makes them inherit from their parent
    # again, which is the default state.
    debug_logging = bool(entry.options.get(CONF_DEBUG_LOGGING))
    log_level = logging.DEBUG if debug_logging else logging.NOTSET
    _LOGGER.setLevel(log_level)
    if debug_logging:
        _LOGGER.debug("Debug logging enabled via settings")

    client = await BrunataApiClient.async_create(
        hass, entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD]
    )
    coordinator = BrunataDataUpdateCoordinator(hass, entry, client)

    # async_config_entry_first_refresh() converts a failed first refresh into
    # ConfigEntryAuthFailed (if raised) or ConfigEntryNotReady (otherwise) —
    # including while the network is still coming up, where HA retries with its
    # own backoff. Both are simply allowed to bubble up.
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        # Setup is being abandoned, so async_unload_entry will never run for
        # this attempt — close the client here or every retry leaks one.
        await client.async_close()
        raise

    entry.runtime_data = coordinator

    async def _handle_scheduled_refresh(now: datetime) -> None:
        """Refresh on the wall clock rather than on a rolling interval."""
        await coordinator.async_refresh()

    # Poll 30 seconds before every new hour. DataUpdateCoordinator's
    # update_interval would drift with whenever HA last started or the
    # integration was last reloaded.
    entry.async_on_unload(
        async_track_time_change(
            hass, _handle_scheduled_refresh, minute=59, second=30
        )
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # No manual update listener: BrunataOptionsFlowHandler subclasses
    # OptionsFlowWithReload, so HA reloads the entry itself when options are
    # saved, which re-runs this function and re-reads the option above.
    return True


async def async_unload_entry(hass: HomeAssistant, entry: BrunataConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: BrunataDataUpdateCoordinator | None = getattr(
            entry, "runtime_data", None
        )
        if coordinator is not None:
            await coordinator.client.async_close()

    return unload_ok


class BrunataDataUpdateCoordinator(DataUpdateCoordinator[dict[str, BrunataMeter]]):
    """Fetch meter data from Brunata on a schedule."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: BrunataConfigEntry,
        client: BrunataApiClient,
    ) -> None:
        """Initialize."""
        self.client = client
        super().__init__(
            hass,
            _LOGGER,
            # Passed explicitly rather than picked up from the ContextVar. It
            # is what lets the coordinator start the reauth flow when
            # ConfigEntryAuthFailed is raised.
            config_entry=entry,
            name=DOMAIN,
            # No update_interval: polling is driven by the wall-clock listener
            # registered in async_setup_entry.
        )

    async def _async_update_data(self) -> dict[str, BrunataMeter]:
        """Fetch data from the API."""
        try:
            return await self.client.async_get_meters()
        except BrunataAuthError as err:
            # Propagates so Home Assistant starts the re-authentication flow.
            raise ConfigEntryAuthFailed(str(err)) from err
        except BrunataConnectionError as err:
            # Keep the previous values so a short outage doesn't blank the
            # sensors and leave a gap in their statistics. On the very first
            # refresh there is nothing to keep, so the failure is surfaced.
            if self.data is not None:
                _LOGGER.info("Cannot reach Brunata — keeping last known values")
                return self.data
            raise UpdateFailed(str(err)) from err
        except BrunataApiError as err:
            raise UpdateFailed(str(err)) from err

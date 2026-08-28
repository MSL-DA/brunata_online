"""The Brunata integration."""

from __future__ import annotations

import logging
from datetime import datetime

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    BrunataApiClient,
    BrunataApiError,
    BrunataAuthError,
    BrunataConnectionError,
    BrunataMeter,
)
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

type BrunataConfigEntry = ConfigEntry[BrunataDataUpdateCoordinator]

# Nothing in this module touches the log level, and nothing should. An options
# flow used to set it from a stored flag, duplicating Home Assistant's own
# "Enable debug logging" button under the three-dot menu. The button does the
# same job and adds a downloadable log for the session, so the option was
# removed rather than kept alongside it. Point users at the button, or at a
# logger: block in configuration.yaml when the setting has to survive a
# restart.


async def async_setup_entry(hass: HomeAssistant, entry: BrunataConfigEntry) -> bool:
    """Set up Brunata from a config entry."""
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


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    entry: BrunataConfigEntry,
    device: dr.DeviceEntry,
) -> bool:
    """Allow deleting a device whose meter Brunata no longer reports.

    Home Assistant only offers the delete button on an integration's devices
    when this function exists. Without it a meter Brunata has dismounted stays
    in the device registry forever: _parse_meters() drops it from the payload,
    so the entity goes unavailable — correct — but the device itself cannot be
    removed by any means short of deleting the whole config entry and setting
    it up again. Meters are replaced every eight to ten years, so that is not
    a hypothetical.

    A device is removable exactly when its meter is absent from the latest
    data. The identifiers are matched against what the coordinator holds
    rather than against the entity registry, because the payload is the thing
    that decides whether the meter still exists.

    A failed update is not a reason to allow anything. On failure the
    coordinator keeps the previous data, so the check is still made against
    real meters — but if that data were ever empty at the same time, this
    would happily agree to delete every device the integration owns. Refusing
    while the last update failed costs the user one poll's wait and removes
    that possibility entirely.

    runtime_data is read defensively for the same reason it is in
    async_unload_entry: it may not be there. Home Assistant's remove handler
    checks that the device exists, that the config entry exists, that the entry
    supports device removal, and that the integration imports — and then calls
    this. It does *not* check that the entry is loaded, so this can run for an
    entry that never set up or has since been unloaded, where reading
    runtime_data directly raises AttributeError instead of refusing cleanly.
    """
    coordinator: BrunataDataUpdateCoordinator | None = getattr(
        entry, "runtime_data", None
    )
    if coordinator is None:
        _LOGGER.debug(
            "Refusing to remove device %s: the config entry is not loaded, so "
            "there is no meter list to check it against",
            device.id,
        )
        return False

    if not coordinator.last_update_success:
        _LOGGER.debug(
            "Refusing to remove device %s: the last update failed, so the "
            "meter list cannot be trusted to be complete",
            device.id,
        )
        return False

    live = {
        (DOMAIN, f"brunata_{meter_id}") for meter_id in (coordinator.data or {})
    }
    return not (device.identifiers & live)


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

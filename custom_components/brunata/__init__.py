"""The Brunata integration."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    BrunataApiClient,
    BrunataApiError,
    BrunataAuthError,
    BrunataConnectionError,
    BrunataMeter,
)
from .const import DEVICE_ID_PREFIX, DOMAIN

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

type BrunataConfigEntry = ConfigEntry[BrunataDataUpdateCoordinator]

# The poll lands in a 60-second window ending at 59:30, so every install has at
# least 30 seconds left before the hour rolls over. That margin is for the
# request itself to finish, not for a retry — there is none. Measured round
# trips are 0.2-1.6 s, so 30 s is generous; it is cheap and it means a slow
# response still lands its reading in the hour it was taken.
_POLL_WINDOW_BASE_MINUTE = 58
_POLL_WINDOW_BASE_SECOND = 30
_POLL_WINDOW_SPREAD_SECONDS = 60

# How long to stay away after an HTTP 429 with no usable Retry-After. One hour
# is the next scheduled poll anyway, so this only bites when Brunata asks for
# longer than that.
_RATE_LIMIT_DEFAULT_BACKOFF = timedelta(hours=1)


def _entry_jitter_seconds(entry_id: str, spread: int = _POLL_WINDOW_SPREAD_SECONDS) -> int:
    """Derive a stable 0..(spread-1) second offset from the entry_id.

    Deterministic per config entry (stable across HA restarts and reloads),
    so a given install always polls at the same wall-clock second — but
    different installs land at different seconds, avoiding every instance
    hitting Brunata's endpoint in the same one-second window.
    """
    digest = hashlib.sha256(entry_id.encode()).hexdigest()
    return int(digest, 16) % spread


def _jittered_poll_time(entry_id: str) -> tuple[int, int]:
    """Return (minute, second) somewhere in the 58:30-59:29 window."""
    total_second = _POLL_WINDOW_BASE_SECOND + _entry_jitter_seconds(entry_id)
    minute = _POLL_WINDOW_BASE_MINUTE + total_second // 60
    second = total_second % 60
    return minute, second

# Nothing in this module touches the log level, and nothing should. An options
# flow used to set it from a stored flag, duplicating Home Assistant's own
# "Enable debug logging" button, which does the same job and adds a
# downloadable log for the session. Point users at that button, or at a
# `logger:` block in configuration.yaml when it has to survive a restart.


async def async_setup_entry(hass: HomeAssistant, entry: BrunataConfigEntry) -> bool:
    """Set up Brunata from a config entry."""
    client = await BrunataApiClient.async_create(
        hass, entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD]
    )
    coordinator = BrunataDataUpdateCoordinator(hass, entry, client)

    # async_config_entry_first_refresh() converts a failed first refresh into
    # ConfigEntryAuthFailed or ConfigEntryNotReady, and HA retries the latter
    # with its own backoff. Both are allowed to bubble up.
    try:
        await coordinator.async_config_entry_first_refresh()
    except BaseException:
        # Setup is being abandoned, so async_unload_entry will never run for
        # this attempt — close the client here or every retry leaks one.
        #
        # BaseException, not Exception: asyncio.CancelledError is a
        # BaseException, and cancellation is exactly what HA does to a setup
        # still retrying when it shuts down. That path used to slip past this
        # handler and leak the httpx client it had just built.
        await client.async_close()
        raise

    entry.runtime_data = coordinator

    async def _handle_scheduled_refresh(now: datetime) -> None:
        """Refresh on the wall clock rather than on a rolling interval."""
        if not coordinator.async_should_poll(now):
            return
        await coordinator.async_refresh()

    # Poll close to the new hour, jittered per config entry so installs don't
    # all hit Brunata's endpoint in the same one-second window. Deterministic
    # per entry_id (stable across restarts) rather than random per restart,
    # so a given install's poll time stays predictable for debugging.
    # DataUpdateCoordinator's update_interval would instead drift with
    # whenever HA last started or the integration was last reloaded.
    jitter_minute, jitter_second = _jittered_poll_time(entry.entry_id)
    entry.async_on_unload(
        async_track_time_change(
            hass,
            _handle_scheduled_refresh,
            minute=jitter_minute,
            second=jitter_second,
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

    Home Assistant only offers the delete button when this function exists.
    Without it a dismounted meter stays in the device registry forever: the
    entity goes unavailable — correct — but the device cannot be removed short
    of deleting the whole config entry and setting it up again. Meters are
    replaced every eight to ten years, so that is not hypothetical.

    A device is removable exactly when its meter is absent from the latest
    data. Identifiers are matched against what the coordinator holds rather
    than against the entity registry, because the payload is what decides
    whether the meter still exists.

    A failed update is not a reason to allow anything: the coordinator keeps
    the previous data, but if that were ever empty at the same time, this would
    happily agree to delete every device the integration owns.

    runtime_data is read defensively for the same reason as in
    async_unload_entry. Home Assistant's remove handler checks that the device
    and entry exist, that removal is supported and that the integration
    imports — but *not* that the entry is loaded, so this can run for an entry
    that never set up, where a direct read raises AttributeError instead of
    refusing cleanly.

    Agreeing to a removal also forgets the meter id, so the sensor platform can
    build the entity again if Brunata ever reports that meter again — see
    known_meter_ids. Only ids we have just agreed to delete are forgotten. An
    id whose entity is merely unavailable must stay, because that entity is
    still registered and a second one with the same unique_id would be
    rejected by the platform.
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
        (DOMAIN, f"{DEVICE_ID_PREFIX}{meter_id}")
        for meter_id in (coordinator.data or {})
    }
    if device.identifiers & live:
        return False

    for domain, identifier in device.identifiers:
        if domain == DOMAIN and identifier.startswith(DEVICE_ID_PREFIX):
            coordinator.known_meter_ids.discard(
                identifier.removeprefix(DEVICE_ID_PREFIX)
            )
    return True


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
        # Meter ids the sensor platform has already built an entity for. Here
        # rather than in a closure inside sensor.py, because
        # async_remove_config_entry_device() has to take an id back out when
        # the device and its entity are gone.
        self.known_meter_ids: set[str] = set()
        # Set when Brunata answers 429. See async_should_poll().
        self._rate_limited_until: datetime | None = None
        super().__init__(
            hass,
            _LOGGER,
            # Passed explicitly rather than picked up from the ContextVar: it
            # is what lets the coordinator start the reauth flow when
            # ConfigEntryAuthFailed is raised.
            config_entry=entry,
            name=DOMAIN,
            # No update_interval: polling is driven by the wall-clock listener
            # registered in async_setup_entry.
        )

    @callback
    def async_should_poll(self, now: datetime) -> bool:
        """Decide whether this scheduled tick is worth a request.

        There is exactly one reason to stay quiet: Brunata answered 429 and
        asked us to wait. That is the one status where retrying quickly is
        actively harmful — the server has just said we are asking too much —
        so it is honoured to the second. Every other tick polls.

        An adaptive schedule was tried here and rolled back: after a run of
        polls whose readings had not moved, it dropped to one poll every four
        hours. It saved requests during quiet periods, but a reading arriving
        in a skipped hour was recorded up to four hours late, and Home
        Assistant attributes consumption to the hour it was *polled*, not the
        hour Brunata dated it. Polling on the hour, every hour, keeps that
        error bounded at one hour. Do not reintroduce the backoff without
        deciding that trade differently on purpose.
        """
        if self._rate_limited_until is not None:
            if now < self._rate_limited_until:
                _LOGGER.debug(
                    "Skipping this poll: Brunata rate-limited us until %s",
                    self._rate_limited_until,
                )
                return False
            self._rate_limited_until = None

        return True

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
            if err.status == 429:
                wait = (
                    timedelta(seconds=err.retry_after)
                    if err.retry_after is not None
                    else _RATE_LIMIT_DEFAULT_BACKOFF
                )
                self._rate_limited_until = dt_util.utcnow() + wait
                _LOGGER.warning(
                    "Brunata rate-limited this integration; not polling again "
                    "until %s",
                    self._rate_limited_until,
                )
            raise UpdateFailed(str(err)) from err

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
    ParseReport,
)
from .const import DEVICE_ID_PREFIX, DOMAIN

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

type BrunataConfigEntry = ConfigEntry[BrunataDataUpdateCoordinator]

# The poll lands somewhere in 58:30-59:29, so every install has at least 31
# seconds left before the hour rolls over. That margin is for the request
# itself to finish, not for a retry — there is none. Measured round trips are
# 0.2-1.6 s, so it is generous; it is cheap and it means a slow response still
# lands its reading in the hour it was taken.
#
# The spread must not push the last possible poll past 59:59.
# _jittered_poll_time() carries seconds into minutes, so a spread of 120 would
# hand async_track_time_change() a minute of 60, which is not a clock time and
# fails at registration — the integration would load and then never poll.
# test_the_poll_window_stays_inside_the_hour holds the three constants to that.
_POLL_WINDOW_BASE_MINUTE = 58
_POLL_WINDOW_BASE_SECOND = 30
_POLL_WINDOW_SPREAD_SECONDS = 60

# How long to stay away after an HTTP 429 with no usable Retry-After.
#
# Deliberately under an hour rather than exactly one. _rate_limited_until is
# set *after* the request has failed, so a full hour lands a fraction of a
# second past the next scheduled tick — which is then skipped, and two hours
# pass between polls instead of one. Home Assistant attributes consumption to
# the hour it polled, so that silently books a reading an hour late: the exact
# cost async_should_poll() explains the adaptive backoff was rolled back over.
# Five minutes of slack is far more than the round trip needs and lets the
# next tick through.
_RATE_LIMIT_DEFAULT_BACKOFF = timedelta(minutes=55)

# And a ceiling on what Brunata can ask for. Retry-After is a number from the
# network: without a cap, a header of 1e12 stops this integration polling for
# thirty thousand years, and the only trace is one warning naming a date in the
# year 33000. A day is far longer than any real rate limit and still recovers
# on its own.
#
# Applied to the seconds, not to a finished timedelta — see the comment at the
# call site in _async_update_data() for why that distinction is load-bearing.
_RATE_LIMIT_MAX_BACKOFF = timedelta(hours=24)


def _entry_jitter_seconds(entry_id: str) -> int:
    """Derive a stable second offset, inside the spread, from the entry_id.

    The range is _POLL_WINDOW_SPREAD_SECONDS and is not written out here: a
    number restated in prose stops matching the constant the moment someone
    changes it, and nothing turns red.

    Deterministic per config entry (stable across HA restarts and reloads),
    so a given install always polls at the same wall-clock second — but
    different installs land at different seconds, avoiding every instance
    hitting Brunata's endpoint in the same one-second window.
    """
    digest = hashlib.sha256(entry_id.encode()).hexdigest()
    return int(digest, 16) % _POLL_WINDOW_SPREAD_SECONDS


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
    """Decide whether a meter's device may be deleted.

    Note what this does *not* control. The delete button on a device page is
    shown as soon as an integration defines this function at all — it is a
    property of the integration, not of the data. This function decides what
    happens when the button is pressed: False makes Home Assistant refuse with
    "Failed to remove device entry, rejected by integration", True deletes the
    device and its entity. So every answer here is a decision about a click
    somebody has already made.

    Without the function at all, a dismounted meter would stay in the device
    registry forever — the entity goes unavailable, correctly, but the device
    could not be removed short of deleting the whole config entry and setting
    it up again. Meters are replaced every eight to ten years, so that is not
    hypothetical.

    **The rule: agree only when the meter is provably gone.** Absence from the
    latest data is not proof on its own, because a meter can be missing from it
    for three different reasons — see ParseReport. Two of them are answered
    here:

    * *Brunata reported nothing at all.* A payload with zero entries parses to
      an empty dictionary and a perfectly successful update, at which point
      every device this integration owns looks dismounted at once. This used to
      be guarded with last_update_success alone, on the reasoning that a failed
      update was the only way `data` could be empty. It is not: an empty
      payload succeeds. raw_item_count is what distinguishes "Brunata listed
      meters and this one was not among them" from "Brunata listed nothing".
    * *The unit did not resolve.* api.py drops such a meter so it cannot become
      an entity carrying a raw code as its unit, but the meter is still on the
      wall. It is counted as present here.

    The cost of the first check is that an account whose meters have *all* been
    dismounted at once, and which Brunata answers for with an empty list rather
    than with dismounted entries, cannot delete them individually. That user
    can still delete the config entry. Refusing a legitimate deletion is
    recoverable; agreeing to a wrong one takes the entity and its long term
    statistics with it, and that cannot be undone.

    A failed update is likewise not a reason to allow anything: the coordinator
    keeps the previous data, and it describes a poll that is no longer the
    latest one.

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

    if not coordinator.last_parse.raw_item_count:
        _LOGGER.debug(
            "Refusing to remove device %s: the last poll returned no meters at "
            "all, which says nothing about this one",
            device.id,
        )
        return False

    # Meters Brunata still reports: the ones that parsed, plus the ones only
    # skipped because their unit did not resolve this poll.
    present = set(coordinator.data or {}) | (
        coordinator.last_parse.unresolved_unit_meter_ids
    )
    live = {(DOMAIN, f"{DEVICE_ID_PREFIX}{meter_id}") for meter_id in present}
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
        # What the last successful poll's parse saw, beyond the meters it
        # produced. A meter absent from `data` is not necessarily gone, and an
        # empty `data` is not necessarily an empty account — see ParseReport,
        # BrunataSensor.available and async_remove_config_entry_device().
        #
        # Starts empty with a raw count of zero, which is the honest state
        # before the first poll: nothing has been reported, so nothing can be
        # concluded.
        self.last_parse = ParseReport(frozenset(), 0)
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
            # Cleared here, and only here. This is the one place the deadline
            # is read, so it expires where it is used rather than being reset
            # by a successful update somewhere else — a second place would have
            # to be kept in step with this one for no gain.
            self._rate_limited_until = None

        return True

    async def _async_update_data(self) -> dict[str, BrunataMeter]:
        """Fetch data from the API."""
        try:
            meters = await self.client.async_get_meters()
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
                # Capped in seconds, not by min()-ing two timedeltas. That
                # version built the timedelta before it compared, so the cap
                # never got a chance to apply: timedelta tops out at 999999999
                # days, and any Retry-After at or above 8.64e13 seconds raised
                # OverflowError right here — inside the very except block that
                # exists to translate this error, so it escaped as an
                # unexpected exception. api.py already rejects inf and nan; a
                # finite number too large for a timedelta is the other half of
                # the same guard, and it belongs here because how long we are
                # willing to stay away is this module's decision.
                wait = (
                    timedelta(
                        seconds=min(
                            err.retry_after,
                            _RATE_LIMIT_MAX_BACKOFF.total_seconds(),
                        )
                    )
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

        # Only on the success path. A failed update leaves the previous report
        # in place, the same way it leaves the previous data in place: the two
        # are read together and must describe the same poll.
        self.last_parse = self.client.last_parse_report()
        return meters

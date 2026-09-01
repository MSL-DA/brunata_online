"""Test Brunata integration setup and teardown."""

import asyncio
import logging
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.brunata import (
    BrunataDataUpdateCoordinator,
    _jittered_poll_time,
    async_remove_config_entry_device,
    async_setup_entry,
)
from custom_components.brunata.api import (
    BrunataApiError,
    BrunataAuthError,
    BrunataConnectionError,
)
from custom_components.brunata.const import DOMAIN


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "test@example.com", "password": "password123"},
    )
    entry.add_to_hass(hass)
    return entry


async def test_setup_entry(hass: HomeAssistant, mock_brunata_client):
    """Test a successful setup."""
    entry = _entry(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    # The coordinator lives on the entry, not in hass.data[DOMAIN].
    assert isinstance(entry.runtime_data, BrunataDataUpdateCoordinator)


async def test_unload_entry_closes_the_client(hass: HomeAssistant, mock_brunata_client):
    """The API client owns an httpx session; without closing it every reload
    leaks keep-alive sockets for the life of the process."""
    entry = _entry(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    mock_brunata_client.async_close.assert_awaited()


async def test_failed_setup_still_closes_the_client(
    hass: HomeAssistant, mock_brunata_client
):
    """Setup is abandoned before runtime_data is set, so async_unload_entry
    never runs for this attempt — without closing here, every retry during a
    slow network start leaks a client."""
    entry = _entry(hass)
    mock_brunata_client.async_get_meters = AsyncMock(
        side_effect=BrunataConnectionError("network not up yet")
    )

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY
    mock_brunata_client.async_close.assert_awaited()


async def test_auth_failure_during_setup_starts_reauth(
    hass: HomeAssistant, mock_brunata_client
):
    """Bad credentials must prompt for re-authentication rather than retry
    forever with a password that will never work."""
    entry = _entry(hass)
    mock_brunata_client.async_get_meters = AsyncMock(
        side_effect=BrunataAuthError("credentials rejected")
    )

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(flow["context"].get("source") == "reauth" for flow in flows)


# --- removing a device whose meter is gone --------------------------------


async def _setup_with_meter(hass: HomeAssistant, mock_brunata_client, mock_meter):
    """Set up the entry with one meter and return the entry and its device."""
    entry = _entry(hass)
    mock_brunata_client.async_get_meters = AsyncMock(
        return_value={"12345": mock_meter}
    )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    device = dr.async_get(hass).async_get_device(
        identifiers={(DOMAIN, "brunata_12345")}
    )
    assert device is not None
    return entry, device


async def test_a_device_whose_meter_is_gone_can_be_removed(
    hass: HomeAssistant, mock_brunata_client, mock_meter
):
    """Home Assistant only offers the delete button when this function exists.

    Without it, a meter Brunata has dismounted leaves a device that cannot be
    removed by any means short of deleting the config entry and setting it up
    again. Meters are replaced every eight to ten years, so it happens.
    """
    entry, device = await _setup_with_meter(hass, mock_brunata_client, mock_meter)

    # Brunata dismounts the meter, so _parse_meters() drops it from the payload.
    mock_brunata_client.async_get_meters = AsyncMock(return_value={})
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert await async_remove_config_entry_device(hass, entry, device) is True


async def test_a_device_whose_meter_still_reports_cannot_be_removed(
    hass: HomeAssistant, mock_brunata_client, mock_meter
):
    """Deleting a live meter's device would recreate it on the next poll and
    lose whatever the user had set on it in the meantime."""
    entry, device = await _setup_with_meter(hass, mock_brunata_client, mock_meter)

    assert await async_remove_config_entry_device(hass, entry, device) is False


async def test_removing_a_device_lets_the_meter_come_back(
    hass: HomeAssistant, mock_brunata_client, mock_meter
):
    """Deleting the device has to let the entity be built again.

    The sensor platform keeps the meter ids it has already created an entity
    for, so it does not add a second one on every poll. That bookkeeping used
    to live in a closure nobody else could reach, so a deleted device left its
    id behind and the meter could never come back without reloading the entry.
    """
    entry, device = await _setup_with_meter(hass, mock_brunata_client, mock_meter)
    coordinator = entry.runtime_data
    assert coordinator.known_meter_ids == {"12345"}

    mock_brunata_client.async_get_meters = AsyncMock(return_value={})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert await async_remove_config_entry_device(hass, entry, device) is True
    assert coordinator.known_meter_ids == set()


async def test_a_meter_that_is_merely_gone_is_not_forgotten(
    hass: HomeAssistant, mock_brunata_client, mock_meter
):
    """The other half, and the trap in the obvious fix.

    A meter absent from one payload still has its entity registered in Home
    Assistant. Forgetting its id would have the platform create a second entity
    with the same unique_id when the meter returns, which the platform rejects
    outright. Only an id whose device has actually been deleted may be dropped.
    """
    entry, _ = await _setup_with_meter(hass, mock_brunata_client, mock_meter)
    coordinator = entry.runtime_data

    mock_brunata_client.async_get_meters = AsyncMock(return_value={})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.known_meter_ids == {"12345"}


async def test_nothing_is_removable_while_the_last_update_failed(
    hass: HomeAssistant, mock_brunata_client, mock_meter
):
    """A failed update leaves the coordinator on its previous data, so the
    check is normally still made against real meters. But if that data were
    ever empty at the same moment, every device the integration owns would
    look dismounted at once. Refusing while the last update failed costs one
    poll's wait and removes the possibility."""
    entry, device = await _setup_with_meter(hass, mock_brunata_client, mock_meter)

    mock_brunata_client.async_get_meters = AsyncMock(
        side_effect=BrunataApiError("Brunata rate limit reached", 429)
    )
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert entry.runtime_data.last_update_success is False
    assert await async_remove_config_entry_device(hass, entry, device) is False


async def test_no_device_is_removable_when_the_entry_is_not_loaded(
    hass: HomeAssistant, mock_brunata_client, mock_meter
):
    """Home Assistant does not require a loaded entry before calling the hook.

    Its remove handler checks that the device exists, that the config entry
    exists, that the entry supports device removal, and that the integration
    imports — then calls this. Reading entry.runtime_data directly would raise
    AttributeError for an entry that never set up or has since been unloaded,
    which reaches the websocket handler as an unhandled exception instead of a
    clean refusal.
    """
    entry, device = await _setup_with_meter(hass, mock_brunata_client, mock_meter)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED

    # The premise, asserted rather than assumed. Home Assistant deletes
    # runtime_data after a successful unload, which is what makes a direct
    # entry.runtime_data raise here. Without this line the test passes whether
    # or not the guard exists: the meter is still in the coordinator's data, so
    # a surviving runtime_data would reach the identifier check and return
    # False for a completely different reason.
    assert not hasattr(entry, "runtime_data")

    assert await async_remove_config_entry_device(hass, entry, device) is False


# --- the polling schedule --------------------------------------------------


async def test_polling_is_driven_by_the_wall_clock(
    hass: HomeAssistant, mock_brunata_client, freezer: FrozenDateTimeFactory
):
    """The coordinator has no update_interval on purpose.

    async_track_time_change ties polling to the wall clock at a per-entry
    jittered (minute, second) in the 58:30-59:29 window, so it lands 30-90
    seconds before each new hour no matter when Home Assistant last started or
    the integration was last reloaded, while different installs land at
    different seconds. The margin is for the request itself to finish inside
    the hour — measured round trips are 0.2-1.6 s — not for a retry, of which
    there is none. An update_interval would drift with the restart time
    instead, and nothing in the suite noticed the difference until this test.
    """
    freezer.move_to("2026-08-27 10:00:00+00:00")
    entry = _entry(hass)
    jitter_minute, jitter_second = _jittered_poll_time(entry.entry_id)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = entry.runtime_data
    assert coordinator.update_interval is None

    calls_after_setup = mock_brunata_client.async_get_meters.call_count

    # A different minute within the same hour is not on the schedule. Using
    # a different minute (rather than one second earlier) avoids a flaky
    # collision on the rare entry_id whose jittered second is 0.
    off_schedule = dt_util.utcnow().replace(
        hour=10, minute=jitter_minute - 1, second=jitter_second, microsecond=0
    )
    freezer.move_to(off_schedule)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert mock_brunata_client.async_get_meters.call_count == calls_after_setup

    # This entry's jittered (minute, second) is on the schedule.
    on_schedule = dt_util.utcnow().replace(
        hour=10, minute=jitter_minute, second=jitter_second, microsecond=0
    )
    freezer.move_to(on_schedule)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert mock_brunata_client.async_get_meters.call_count == calls_after_setup + 1


async def test_unloading_stops_the_schedule(
    hass: HomeAssistant, mock_brunata_client, freezer: FrozenDateTimeFactory
):
    """The listener is registered through entry.async_on_unload, so an unloaded
    entry must stop polling. Without that, every reload leaves another timer
    calling into a coordinator nobody is listening to."""
    freezer.move_to("2026-08-27 10:00:00+00:00")
    entry = _entry(hass)
    jitter_minute, jitter_second = _jittered_poll_time(entry.entry_id)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    calls_after_unload = mock_brunata_client.async_get_meters.call_count

    on_schedule = dt_util.utcnow().replace(
        hour=10, minute=jitter_minute, second=jitter_second, microsecond=0
    )
    freezer.move_to(on_schedule)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()

    assert mock_brunata_client.async_get_meters.call_count == calls_after_unload


# --- staying away only when Brunata asks -----------------------------------


async def test_unchanged_readings_do_not_slow_the_schedule(
    hass: HomeAssistant, mock_brunata_client, mock_meter
):
    """The schedule is unconditional: every hour, whatever the payload said.

    An adaptive backoff after a run of unchanged readings was tried and rolled
    back — see async_should_poll(). It is cheap to reintroduce by accident, and
    the cost is invisible in normal use: readings would still arrive, just
    attributed to a later hour than the one they belong to. This test is what
    makes that regression fail loudly instead of silently.
    """
    entry, _ = await _setup_with_meter(hass, mock_brunata_client, mock_meter)
    coordinator = entry.runtime_data
    now = dt_util.utcnow()

    # Well past any threshold a backoff would plausibly use.
    for _ in range(12):
        await coordinator.async_refresh()
        assert coordinator.async_should_poll(now) is True


async def test_a_rate_limit_is_honoured_to_the_second(
    hass: HomeAssistant, mock_brunata_client, mock_meter
):
    """429 is the one status where retrying quickly is actively harmful.

    Brunata has just said we are asking too much, so Retry-After is obeyed
    rather than treated as one more failed update. With an hourly schedule
    this only bites when Brunata asks for longer than an hour — which is
    exactly the case worth getting right.
    """
    entry, _ = await _setup_with_meter(hass, mock_brunata_client, mock_meter)
    coordinator = entry.runtime_data

    mock_brunata_client.async_get_meters = AsyncMock(
        side_effect=BrunataApiError("Brunata rate limit reached (429)", 429, 7200)
    )
    await coordinator.async_refresh()
    assert coordinator.last_update_success is False

    now = dt_util.utcnow()
    assert coordinator.async_should_poll(now + timedelta(hours=1)) is False
    assert coordinator.async_should_poll(now + timedelta(hours=3)) is True


async def test_a_rate_limit_without_retry_after_lets_the_next_tick_through(
    hass: HomeAssistant, mock_brunata_client, mock_meter
):
    """The default backoff must clear the next scheduled poll, not land past it.

    The baseline is taken *before* the refresh on purpose. _rate_limited_until
    is set after the request has failed, so the next tick — exactly one hour
    after the tick that failed — is what has to be tested against. An earlier
    version of this test anchored on dt_util.utcnow() after the refresh and
    checked 30 and 61 minutes, which cannot see the boundary at all: with a
    one-hour default the tick an hour later fell a fraction of a second short
    and was skipped, so two hours passed between polls and a reading was
    booked an hour late.
    """
    entry, _ = await _setup_with_meter(hass, mock_brunata_client, mock_meter)
    coordinator = entry.runtime_data

    tick = dt_util.utcnow()
    mock_brunata_client.async_get_meters = AsyncMock(
        side_effect=BrunataApiError("Brunata rate limit reached (429)", 429)
    )
    await coordinator.async_refresh()

    # Well inside the backoff.
    assert coordinator.async_should_poll(tick + timedelta(minutes=30)) is False
    # The next hourly tick, measured from the one that failed.
    assert coordinator.async_should_poll(tick + timedelta(hours=1)) is True


@pytest.mark.parametrize("retry_after", [1e12, 8.64e13, 1e15, 1e300])
async def test_a_rate_limit_longer_than_a_day_is_capped(
    hass: HomeAssistant, mock_brunata_client, mock_meter, retry_after
):
    """Retry-After is a number from the network.

    Without a ceiling, a header of 1e12 stops this integration polling for
    thirty thousand years, and the only trace is one warning naming a date in
    the year 33000. A day is longer than any real rate limit and recovers on
    its own.

    The values above 8.64e13 are the ones that matter, and the reason the cap
    is applied to the seconds rather than to a finished timedelta. timedelta
    tops out at 999999999 days, so an earlier version — which built
    timedelta(seconds=err.retry_after) and only then compared it against the
    ceiling — raised OverflowError inside the except block that exists to
    translate this error, and it escaped as an unexpected exception. api.py's
    guard does not catch these: every one of them is finite.

    1e12 is kept because it is the one that fits in a timedelta, so it would
    pass either way. Alone, it could not tell the two versions apart.
    """
    entry, _ = await _setup_with_meter(hass, mock_brunata_client, mock_meter)
    coordinator = entry.runtime_data

    mock_brunata_client.async_get_meters = AsyncMock(
        side_effect=BrunataApiError(
            "Brunata rate limit reached (429)", 429, retry_after
        )
    )
    await coordinator.async_refresh()

    now = dt_util.utcnow()
    assert coordinator.async_should_poll(now + timedelta(hours=23)) is False
    assert coordinator.async_should_poll(now + timedelta(hours=25)) is True


async def test_other_errors_do_not_hold_the_schedule(
    hass: HomeAssistant, mock_brunata_client, mock_meter
):
    """Only 429 is a request to stay away. A 500 is Brunata having a bad day,
    and the next hourly poll is the right response to that."""
    entry, _ = await _setup_with_meter(hass, mock_brunata_client, mock_meter)
    coordinator = entry.runtime_data

    mock_brunata_client.async_get_meters = AsyncMock(
        side_effect=BrunataApiError("Brunata server error (503)", 503)
    )
    await coordinator.async_refresh()

    assert coordinator.async_should_poll(dt_util.utcnow()) is True


# --- the log level is not ours to set --------------------------------------


async def test_setup_does_not_touch_the_log_level(
    hass: HomeAssistant, mock_brunata_client
):
    """Setting up must leave custom_components.brunata's level alone.

    An options flow used to write it from a stored flag, duplicating Home
    Assistant's own "Enable debug logging" button. The option was removed
    rather than kept alongside it, and this test is what keeps it removed: a
    reintroduction would make the button's effect depend on whether the entry
    happened to reload afterwards.
    """
    logger = logging.getLogger("custom_components.brunata")
    original = logger.level
    try:
        logger.setLevel(logging.DEBUG)

        entry = _entry(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert logger.level == logging.DEBUG

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        assert logger.level == logging.DEBUG
    finally:
        logger.setLevel(original)


# --- abandoning setup must not leak the client -----------------------------


async def test_a_cancelled_setup_still_closes_the_client(
    hass: HomeAssistant, mock_brunata_client
):
    """asyncio.CancelledError is a BaseException, not an Exception.

    Home Assistant cancels a setup that is still retrying when it shuts down,
    and that path used to slip past the `except Exception` around the first
    refresh — leaking the httpx client that had just been built, once per
    attempt. The handler is `except BaseException` for exactly this.

    The coordinator method is patched rather than the API mock, so the
    cancellation lands in the same place a real one would without depending on
    how DataUpdateCoordinator handles it on the way through.
    """
    entry = _entry(hass)

    with patch.object(
        BrunataDataUpdateCoordinator,
        "async_config_entry_first_refresh",
        side_effect=asyncio.CancelledError,
    ), pytest.raises(asyncio.CancelledError):
        await async_setup_entry(hass, entry)

    assert mock_brunata_client.async_close.called

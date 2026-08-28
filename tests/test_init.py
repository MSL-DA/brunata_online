"""Test Brunata integration setup and teardown."""

import asyncio
import logging
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

    assert await async_remove_config_entry_device(hass, entry, device) is False


# --- the polling schedule --------------------------------------------------


async def test_polling_is_driven_by_the_wall_clock(
    hass: HomeAssistant, mock_brunata_client, freezer: FrozenDateTimeFactory
):
    """The coordinator has no update_interval on purpose.

    async_track_time_change(minute=59, second=30) ties polling to the wall
    clock, so it lands 30 seconds before each new hour no matter when Home
    Assistant last started or the integration was last reloaded. An
    update_interval would drift with the restart time instead, and nothing in
    the suite noticed the difference until this test.
    """
    freezer.move_to("2026-08-27 10:00:00+00:00")
    entry = _entry(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = entry.runtime_data
    assert coordinator.update_interval is None

    calls_after_setup = mock_brunata_client.async_get_meters.call_count

    # 10:30 is not on the schedule.
    freezer.move_to("2026-08-27 10:30:00+00:00")
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert mock_brunata_client.async_get_meters.call_count == calls_after_setup

    # 10:59:30 is.
    freezer.move_to("2026-08-27 10:59:30+00:00")
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

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    calls_after_unload = mock_brunata_client.async_get_meters.call_count

    freezer.move_to("2026-08-27 10:59:30+00:00")
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()

    assert mock_brunata_client.async_get_meters.call_count == calls_after_unload


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

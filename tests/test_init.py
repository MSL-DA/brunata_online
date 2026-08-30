"""Test Brunata integration setup and teardown."""

from unittest.mock import AsyncMock

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.brunata import (
    BrunataDataUpdateCoordinator,
    _jittered_poll_time,
)
from custom_components.brunata.api import BrunataAuthError, BrunataConnectionError
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


# --- the polling schedule --------------------------------------------------
#
# The schedule is jittered per config entry (see _jittered_poll_time) so
# installs don't all hit Brunata's endpoint in the same one-second window.
# The offset is deterministic from entry.entry_id, so these tests compute the
# expected trigger time from the test entry's own entry_id rather than
# hardcoding a single wall-clock second — a fixed second would only
# coincidentally match whatever MockConfigEntry happens to generate.


async def test_polling_is_driven_by_the_wall_clock(
    hass: HomeAssistant, mock_brunata_client, freezer: FrozenDateTimeFactory
):
    """The coordinator has no update_interval on purpose.

    async_track_time_change ties polling to the wall clock at a per-entry
    jittered (minute, second) close to the new hour, so it lands roughly
    30-90 seconds before each new hour no matter when Home Assistant last
    started or the integration was last reloaded, while different installs
    land at different seconds. An update_interval would drift with the
    restart time instead, and nothing in the suite noticed the difference
    until this test.
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

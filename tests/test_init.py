"""Test Brunata integration setup and teardown."""

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.brunata import BrunataDataUpdateCoordinator
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


async def test_debug_logging_option_is_applied_on_setup(
    hass: HomeAssistant, mock_brunata_client
):
    """The option is read on every setup, which is how OptionsFlowWithReload
    makes it take effect without a restart."""
    import logging

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "test@example.com", "password": "password123"},
        options={"debug_logging": True},
    )
    entry.add_to_hass(hass)

    with patch("custom_components.brunata._LOGGER") as logger:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    logger.setLevel.assert_called_with(logging.DEBUG)

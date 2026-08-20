"""Tests for how the coordinator translates API errors.

The fetching and parsing now live in api.py; all the coordinator does is map
the API layer's error types onto Home Assistant's. That mapping is what decides
whether the user gets a re-authentication prompt, a quiet retry, or sensors
frozen at their last value, so each branch is checked here.
"""

from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.brunata import BrunataDataUpdateCoordinator
from custom_components.brunata.api import (
    BrunataApiError,
    BrunataAuthError,
    BrunataConnectionError,
)
from custom_components.brunata.const import DOMAIN


@pytest.fixture
def coordinator(hass: HomeAssistant, mock_brunata_client):
    """A coordinator wired to the mocked client, without running setup."""
    entry = MockConfigEntry(
        domain=DOMAIN, data={"email": "test@example.com", "password": "password123"}
    )
    entry.add_to_hass(hass)
    return BrunataDataUpdateCoordinator(hass, entry, mock_brunata_client)


async def test_successful_update_returns_the_meters(
    coordinator, mock_brunata_client, mock_meter
):
    mock_brunata_client.async_get_meters = AsyncMock(
        return_value={"12345": mock_meter}
    )

    assert await coordinator._async_update_data() == {"12345": mock_meter}


async def test_auth_error_triggers_reauth(coordinator, mock_brunata_client):
    """ConfigEntryAuthFailed is what makes Home Assistant open the reauth
    flow; UpdateFailed would just retry forever with a dead password."""
    mock_brunata_client.async_get_meters = AsyncMock(
        side_effect=BrunataAuthError("credentials rejected")
    )

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_connection_error_keeps_last_known_values(
    coordinator, mock_brunata_client, mock_meter
):
    """A short outage must not blank the sensors, or their statistics get a gap
    for every hour Brunata is unreachable."""
    previous = {"12345": mock_meter}
    coordinator.data = previous
    mock_brunata_client.async_get_meters = AsyncMock(
        side_effect=BrunataConnectionError("down")
    )

    assert await coordinator._async_update_data() is previous


async def test_connection_error_on_first_update_fails(
    coordinator, mock_brunata_client
):
    """With no previous data there is nothing to keep, so the failure has to be
    surfaced — otherwise setup would 'succeed' with zero sensors."""
    coordinator.data = None
    mock_brunata_client.async_get_meters = AsyncMock(
        side_effect=BrunataConnectionError("down")
    )

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_api_error_fails_the_update(coordinator, mock_brunata_client, mock_meter):
    """Unlike a connection error, a bad response is not something waiting will
    fix, so it is reported rather than papered over with stale data."""
    coordinator.data = {"12345": mock_meter}
    mock_brunata_client.async_get_meters = AsyncMock(
        side_effect=BrunataApiError("Brunata rate limit reached", 429)
    )

    with pytest.raises(UpdateFailed, match="rate limit"):
        await coordinator._async_update_data()

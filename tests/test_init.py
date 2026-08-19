"""Test Brunata integration setup."""
import logging
from unittest.mock import AsyncMock, MagicMock, call, patch
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from custom_components.brunata import BrunataDataUpdateCoordinator
from custom_components.brunata.const import DOMAIN
from pytest_homeassistant_custom_component.common import MockConfigEntry

async def test_setup_entry(hass: HomeAssistant, mock_brunata_client):
    """Test setting up the integration."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "email": "test@example.com",
            "password": "password123",
        },
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.brunata._check_connectivity",
        AsyncMock(return_value=True),
    ), patch(
        "custom_components.brunata.BrunataDataUpdateCoordinator._async_update_data",
        return_value={},
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert DOMAIN in hass.data
    assert entry.entry_id in hass.data[DOMAIN]

async def test_unload_entry(hass: HomeAssistant, mock_brunata_client):
    """Test unloading the integration."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "email": "test@example.com",
            "password": "password123",
        },
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.brunata._check_connectivity",
        AsyncMock(return_value=True),
    ), patch(
        "custom_components.brunata.BrunataDataUpdateCoordinator._async_update_data",
        return_value={},
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.entry_id not in hass.data[DOMAIN]


async def test_coordinator_fetches_meter_data(hass: HomeAssistant, mock_brunata_client, mock_meter):
    """Test that the coordinator pulls meter data from the API."""
    mock_brunata_client._meters = {}
    mock_brunata_client._get_tokens = AsyncMock(return_value=None)
    mock_brunata_client._init_mappers = AsyncMock(return_value=None)

    api_response = [
        {
            "meter": {
                "meterId": "12345",
                "meterNo": "M12345",
                "meterType": "Heat",
                "meterUnit": "kWh",
                "superAllocationUnit": 1,
            },
            "reading": {"value": 100.5, "readingDate": "2024-01-01"},
        }
    ]

    response = MagicMock()
    response.status_code = 200
    response.json.return_value = api_response
    response.text = str(api_response)
    mock_brunata_client.api_wrapper = AsyncMock(return_value=response)

    with patch("custom_components.brunata.Meter", return_value=mock_meter):
        coordinator = BrunataDataUpdateCoordinator(hass, mock_brunata_client)
        meter_data = await coordinator._async_update_data()

    assert "12345" in meter_data
    assert meter_data["12345"] is mock_meter
    mock_meter.add_reading.assert_called_once_with(api_response[0]["reading"])


async def test_coordinator_retries_with_fresh_login_after_401_then_succeeds(
    hass: HomeAssistant, mock_brunata_client, mock_meter
):
    """A 401 with a (locally) still-valid cached token should trigger exactly
    one forced re-login and retry — not an immediate ConfigEntryAuthFailed —
    per https://github.com/MSL-DA/brunata_online 401-after-cached-token report."""
    mock_brunata_client._meters = {}
    mock_brunata_client._get_tokens = AsyncMock(return_value=None)
    mock_brunata_client._init_mappers = AsyncMock(return_value=None)

    api_response = [
        {
            "meter": {
                "meterId": "12345",
                "meterNo": "M12345",
                "meterType": "Heat",
                "meterUnit": "kWh",
                "superAllocationUnit": 1,
            },
            "reading": {"value": 100.5, "readingDate": "2024-01-01"},
        }
    ]

    unauthorized_response = MagicMock()
    unauthorized_response.status_code = 401

    ok_response = MagicMock()
    ok_response.status_code = 200
    ok_response.json.return_value = api_response
    ok_response.text = str(api_response)

    mock_brunata_client.api_wrapper = AsyncMock(
        side_effect=[unauthorized_response, ok_response]
    )

    with patch("custom_components.brunata.Meter", return_value=mock_meter):
        coordinator = BrunataDataUpdateCoordinator(hass, mock_brunata_client)
        meter_data = await coordinator._async_update_data()

    assert "12345" in meter_data
    assert mock_brunata_client.api_wrapper.call_count == 2
    # First attempt reuses whatever cached token _get_tokens() decides on its
    # own (force=False); the retry after the 401 must force a brand-new login.
    assert mock_brunata_client._get_tokens.call_args_list == [
        call(force=False),
        call(force=True),
    ]


async def test_coordinator_raises_auth_failed_when_fresh_login_still_401(
    hass: HomeAssistant, mock_brunata_client
):
    """If the API still returns 401/403 after a forced fresh Keycloak login,
    the credentials themselves are no longer valid — HA should be told via
    ConfigEntryAuthFailed so it prompts for re-authentication. There must be
    only one retry, not an unbounded loop."""
    mock_brunata_client._meters = {}
    mock_brunata_client._get_tokens = AsyncMock(return_value=None)
    mock_brunata_client._init_mappers = AsyncMock(return_value=None)

    unauthorized_response = MagicMock()
    unauthorized_response.status_code = 401
    mock_brunata_client.api_wrapper = AsyncMock(return_value=unauthorized_response)

    coordinator = BrunataDataUpdateCoordinator(hass, mock_brunata_client)

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()

    assert mock_brunata_client.api_wrapper.call_count == 2
    assert mock_brunata_client._get_tokens.call_args_list == [
        call(force=False),
        call(force=True),
    ]

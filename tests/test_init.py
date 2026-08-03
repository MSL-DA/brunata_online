"""Test Brunata integration setup."""
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
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
    response.json.return_value = api_response
    response.text = str(api_response)
    mock_brunata_client.api_wrapper = AsyncMock(return_value=response)

    with patch("brunata_api.Meter", return_value=mock_meter):
        coordinator = BrunataDataUpdateCoordinator(hass, mock_brunata_client)
        meter_data = await coordinator._async_update_data()

    assert "12345" in meter_data
    assert meter_data["12345"] is mock_meter
    mock_meter.add_reading.assert_called_once_with(api_response[0]["reading"])

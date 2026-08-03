"""Test Brunata sensor."""
from unittest.mock import patch, MagicMock
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from custom_components.brunata.const import DOMAIN
from pytest_homeassistant_custom_component.common import MockConfigEntry

async def test_sensor_setup(hass: HomeAssistant, mock_brunata_client, mock_meter):
    """Test sensor setup and state."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "email": "test@example.com",
            "password": "password123",
        },
    )
    entry.add_to_hass(hass)

    mock_brunata_client._meters = {"12345": mock_meter}
    
    # Mock DataUpdateCoordinator._async_update_data to return the mock meters
    with patch(
        "custom_components.brunata.BrunataDataUpdateCoordinator._async_update_data",
        return_value={"12345": mock_meter},
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    entity_id = next(
        (
            entity_id
            for entity_id in hass.states.async_entity_ids(SENSOR_DOMAIN)
            if entity_id.endswith("_consumption")
        ),
        None,
    )
    assert entity_id is not None

    state = hass.states.get(entity_id)
    assert state.attributes["friendly_name"] == "Brunata Heat (12345) Consumption"
    assert state.attributes["reading_date"] == "2024-01-01"

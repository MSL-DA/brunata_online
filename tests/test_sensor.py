"""Test Brunata sensor."""
from unittest.mock import patch, MagicMock, AsyncMock
from homeassistant.core import HomeAssistant
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from datetime import date
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
        "custom_components.brunata._check_connectivity",
        AsyncMock(return_value=True),
    ), patch(
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
    assert state.attributes["reading_date"] == date(2024, 1, 1)


async def test_sensor_reset_detection(mock_meter):
    """Test that a decrease is accepted on Dec 31/Jan 1 but rejected otherwise."""
    from custom_components.brunata.sensor import BrunataSensor

    mock_meter.meter_type = "Radiator"
    mock_meter.meter_unit = ""

    coordinator = MagicMock()
    coordinator.data = {"12345": mock_meter}
    with patch("homeassistant.helpers.update_coordinator.CoordinatorEntity.__init__", return_value=None):
        entity = BrunataSensor(coordinator, mock_meter)
    entity.coordinator = coordinator

    # Initial reading (accepted: first value ever seen).
    mock_meter.latest_reading.value = 500.0
    mock_meter.latest_reading.date = date(2024, 12, 30)
    assert entity.native_value == 500.0

    # Decrease outside the reset window (Dec 31/Jan 1) — must be rejected as a glitch.
    mock_meter.latest_reading.value = 10.0
    mock_meter.latest_reading.date = date(2024, 6, 15)
    assert entity.native_value == 500.0

    # Decrease on the reset window — must be accepted as a real year-end reset.
    mock_meter.latest_reading.value = 10.0
    mock_meter.latest_reading.date = date(2024, 12, 31)
    assert entity.native_value == 10.0

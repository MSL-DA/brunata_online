"""Test Brunata sensor."""
from unittest.mock import patch, MagicMock, AsyncMock
from homeassistant.core import HomeAssistant, State
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from datetime import date
from custom_components.brunata.const import DOMAIN
from pytest_homeassistant_custom_component.common import MockConfigEntry, mock_restore_cache

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


async def test_sensor_restores_last_state_before_coordinator_has_data(
    hass: HomeAssistant, mock_brunata_client, mock_meter
):
    """A restarted/reloaded sensor must restore its last reading and stay
    available even if the coordinator has not delivered a fresh reading yet —
    this is the exact gap async_added_to_hass()'s restore closes."""
    # has_entity_name + the device name determine the generated entity_id
    # (it includes the meter type, e.g. "heat") rather than
    # _attr_suggested_object_id, confirmed against the actual HA-registered
    # entity_id in test runs.
    entity_id = "sensor.brunata_heat_12345_consumption"
    mock_restore_cache(
        hass,
        [State(entity_id, "500.0", {"reading_date": "2024-12-31"})],
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "email": "test@example.com",
            "password": "password123",
        },
    )
    entry.add_to_hass(hass)

    mock_brunata_client._meters = {"12345": mock_meter}
    # Simulate no fresh reading being available yet at startup.
    mock_meter.latest_reading = None

    with patch(
        "custom_components.brunata._check_connectivity",
        AsyncMock(return_value=True),
    ), patch(
        "custom_components.brunata.BrunataDataUpdateCoordinator._async_update_data",
        return_value={"12345": mock_meter},
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "500.0"
    assert state.attributes["reading_date"] == "2024-12-31"


async def test_sensor_restore_edge_cases(mock_meter):
    """async_added_to_hass must tolerate a missing, unknown/unavailable, or
    non-numeric previous state without raising, and only accept a genuinely
    valid numeric state."""
    from custom_components.brunata.sensor import BrunataSensor

    coordinator = MagicMock()
    coordinator.data = {"12345": mock_meter}
    with patch("homeassistant.helpers.update_coordinator.CoordinatorEntity.__init__", return_value=None):
        entity = BrunataSensor(coordinator, mock_meter)
    entity.coordinator = coordinator

    # No previous state at all (e.g. first ever start).
    with patch(
        "homeassistant.helpers.update_coordinator.CoordinatorEntity.async_added_to_hass",
        AsyncMock(),
    ), patch.object(entity, "async_get_last_state", AsyncMock(return_value=None)):
        await entity.async_added_to_hass()
    assert entity._last_value is None

    # Previous state was unavailable/unknown — must not be restored as a value.
    for bogus_state in ("unavailable", "unknown"):
        with patch(
            "homeassistant.helpers.update_coordinator.CoordinatorEntity.async_added_to_hass",
            AsyncMock(),
        ), patch.object(
            entity,
            "async_get_last_state",
            AsyncMock(return_value=MagicMock(state=bogus_state, attributes={})),
        ):
            await entity.async_added_to_hass()
        assert entity._last_value is None

    # Corrupt/non-numeric previous state must be ignored, not raise.
    with patch(
        "homeassistant.helpers.update_coordinator.CoordinatorEntity.async_added_to_hass",
        AsyncMock(),
    ), patch.object(
        entity,
        "async_get_last_state",
        AsyncMock(return_value=MagicMock(state="not-a-number", attributes={})),
    ):
        await entity.async_added_to_hass()
    assert entity._last_value is None

    # A genuinely valid previous state must be restored.
    with patch(
        "homeassistant.helpers.update_coordinator.CoordinatorEntity.async_added_to_hass",
        AsyncMock(),
    ), patch.object(
        entity,
        "async_get_last_state",
        AsyncMock(return_value=MagicMock(state="42.5", attributes={"reading_date": "2024-06-01"})),
    ):
        await entity.async_added_to_hass()
    assert entity._last_value == 42.5
    assert entity._last_reading_date == "2024-06-01"

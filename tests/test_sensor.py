"""Test Brunata sensor."""
from unittest.mock import patch, MagicMock, AsyncMock
from homeassistant.core import HomeAssistant, State
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from datetime import date
from custom_components.brunata.const import DOMAIN
from pytest_homeassistant_custom_component.common import MockConfigEntry, mock_restore_cache

def _make_entity(coordinator, meter):
    """Build a BrunataSensor without going through CoordinatorEntity.__init__."""
    from custom_components.brunata.sensor import BrunataSensor

    with patch(
        "homeassistant.helpers.update_coordinator.CoordinatorEntity.__init__",
        return_value=None,
    ):
        entity = BrunataSensor(coordinator, meter)
    entity.coordinator = coordinator
    return entity


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
    # Always an ISO string, whether the value came from the API or from a
    # restored state after a restart.
    assert state.attributes["reading_date"] == "2024-01-01"


async def test_sensor_unit_is_normalised(mock_meter):
    """A unit reported with unexpected casing must still resolve to the
    canonical Home Assistant unit. Passing the raw string through would give
    e.g. device_class 'energy' with unit 'KWH', which HA rejects and whose
    long term statistics are then discarded."""
    from homeassistant.components.sensor import SensorDeviceClass
    from homeassistant.const import UnitOfEnergy, UnitOfVolume

    coordinator = MagicMock()
    coordinator.data = {"12345": mock_meter}

    for raw_unit, expected_unit, expected_class in (
        ("KWH", UnitOfEnergy.KILO_WATT_HOUR, SensorDeviceClass.ENERGY),
        ("kWh", UnitOfEnergy.KILO_WATT_HOUR, SensorDeviceClass.ENERGY),
        ("l", UnitOfVolume.LITERS, SensorDeviceClass.WATER),
        ("m3", UnitOfVolume.CUBIC_METERS, SensorDeviceClass.WATER),
        ("m³", UnitOfVolume.CUBIC_METERS, SensorDeviceClass.WATER),
    ):
        mock_meter.meter_unit = raw_unit
        entity = _make_entity(coordinator, mock_meter)
        assert entity.native_unit_of_measurement == expected_unit
        assert entity.device_class == expected_class

    # An unrecognised unit is passed through, but must not claim a device
    # class HA would then reject.
    mock_meter.meter_unit = "widgets"
    entity = _make_entity(coordinator, mock_meter)
    assert entity.native_unit_of_measurement == "widgets"
    assert entity.device_class is None


async def test_sensor_reset_detection(mock_meter):
    """Heat cost allocators are zeroed on 1 January, so a decrease at the turn
    of the year is real. A decrease at any other time is an API glitch."""
    mock_meter.meter_type = "Radiator"
    mock_meter.meter_unit = ""

    coordinator = MagicMock()
    coordinator.data = {"12345": mock_meter}
    entity = _make_entity(coordinator, mock_meter)

    # Last reading of the accounting year.
    mock_meter.latest_reading.value = 4820.0
    mock_meter.latest_reading.date = date(2024, 12, 20)
    entity._apply_latest_reading()
    assert entity.native_value == 4820.0

    # Decrease outside the reset window — must be rejected as a glitch.
    mock_meter.latest_reading.value = 10.0
    mock_meter.latest_reading.date = date(2024, 6, 15)
    entity._apply_latest_reading()
    assert entity.native_value == 4820.0

    # The 1 January reset — must be accepted.
    mock_meter.latest_reading.value = 0.0
    mock_meter.latest_reading.date = date(2025, 1, 1)
    entity._apply_latest_reading()
    assert entity.native_value == 0.0
    assert entity.extra_state_attributes["reading_date"] == "2025-01-01"


async def test_sensor_reset_accepted_when_first_reading_arrives_late(mock_meter):
    """Allocators report infrequently, so the first reading after the 1 January
    reset is not necessarily dated 1 January. Matching only 31 Dec / 1 Jan
    rejected those, and since the cached value is never lowered the sensor then
    stayed frozen at the pre-reset value for the rest of the year."""
    mock_meter.meter_type = "Radiator"
    mock_meter.meter_unit = ""

    coordinator = MagicMock()
    coordinator.data = {"12345": mock_meter}

    # Mid-January: inside the December/January window.
    entity = _make_entity(coordinator, mock_meter)
    mock_meter.latest_reading.value = 4820.0
    mock_meter.latest_reading.date = date(2024, 12, 20)
    entity._apply_latest_reading()
    mock_meter.latest_reading.value = 11.0
    mock_meter.latest_reading.date = date(2025, 1, 17)
    entity._apply_latest_reading()
    assert entity.native_value == 11.0

    # February: outside the window, but the calendar year has advanced since
    # the last accepted reading, which is the reliable signal.
    entity = _make_entity(coordinator, mock_meter)
    mock_meter.latest_reading.value = 4820.0
    mock_meter.latest_reading.date = date(2024, 12, 20)
    entity._apply_latest_reading()
    mock_meter.latest_reading.value = 11.0
    mock_meter.latest_reading.date = date(2025, 2, 3)
    entity._apply_latest_reading()
    assert entity.native_value == 11.0


async def test_sensor_decrease_never_accepted_for_non_resetting_meter(mock_meter):
    """Water and energy meters are never reset, so a decrease is always a
    glitch — including in January, where a radiator meter would accept it."""
    mock_meter.meter_type = "Water"
    mock_meter.meter_unit = "m3"

    coordinator = MagicMock()
    coordinator.data = {"12345": mock_meter}
    entity = _make_entity(coordinator, mock_meter)

    mock_meter.latest_reading.value = 312.5
    mock_meter.latest_reading.date = date(2025, 1, 2)
    entity._apply_latest_reading()
    assert entity.native_value == 312.5

    mock_meter.latest_reading.value = 0.0
    mock_meter.latest_reading.date = date(2025, 1, 3)
    entity._apply_latest_reading()
    assert entity.native_value == 312.5

    mock_meter.latest_reading.value = 313.0
    mock_meter.latest_reading.date = date(2025, 1, 4)
    entity._apply_latest_reading()
    assert entity.native_value == 313.0


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
    coordinator = MagicMock()
    coordinator.data = {"12345": mock_meter}
    # No fresh reading, so only the restored state can set the value.
    mock_meter.latest_reading = None

    async def _restore(entity, last_state):
        with patch(
            "homeassistant.helpers.update_coordinator.CoordinatorEntity.async_added_to_hass",
            AsyncMock(),
        ), patch.object(
            entity, "async_get_last_state", AsyncMock(return_value=last_state)
        ):
            await entity.async_added_to_hass()

    # No previous state at all (e.g. first ever start).
    entity = _make_entity(coordinator, mock_meter)
    await _restore(entity, None)
    assert entity.native_value is None
    assert entity.available is False

    # Previous state was unavailable/unknown — must not be restored as a value.
    for bogus_state in ("unavailable", "unknown"):
        entity = _make_entity(coordinator, mock_meter)
        await _restore(entity, MagicMock(state=bogus_state, attributes={}))
        assert entity.native_value is None

    # Corrupt/non-numeric previous state must be ignored, not raise.
    entity = _make_entity(coordinator, mock_meter)
    await _restore(entity, MagicMock(state="not-a-number", attributes={}))
    assert entity.native_value is None

    # A genuinely valid previous state must be restored.
    entity = _make_entity(coordinator, mock_meter)
    await _restore(
        entity,
        MagicMock(state="42.5", attributes={"reading_date": "2024-06-01"}),
    )
    assert entity.native_value == 42.5
    assert entity.extra_state_attributes["reading_date"] == "2024-06-01"
    assert entity.available is True

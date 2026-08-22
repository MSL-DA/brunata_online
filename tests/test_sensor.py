"""Test Brunata sensor."""
from dataclasses import replace
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN, SensorDeviceClass
from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant, State
from pytest_homeassistant_custom_component.common import MockConfigEntry, mock_restore_cache

from custom_components.brunata.const import DOMAIN
from custom_components.brunata.sensor import BrunataSensor, FALLBACK_UNIT


def _make_entity(coordinator, meter):
    """Build a BrunataSensor without going through CoordinatorEntity.__init__."""
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

    mock_brunata_client.async_get_meters = AsyncMock(
        return_value={"12345": mock_meter}
    )

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
    assert state.attributes["friendly_name"] == "Heat (12345) Consumption"
    # Always an ISO string, whether the value came from the API or from a
    # restored state after a restart.
    assert state.attributes["reading_date"] == "2024-01-01"


async def test_sensor_unit_is_normalised(mock_meter):
    """A unit reported with unexpected casing must still resolve to the
    canonical Home Assistant unit. Passing the raw string through would give
    e.g. device_class 'energy' with unit 'KWH', which HA rejects and whose
    long term statistics are then discarded."""
    coordinator = MagicMock()
    coordinator.data = {"12345": mock_meter}

    for raw_unit, expected_unit, expected_class in (
        ("KWH", UnitOfEnergy.KILO_WATT_HOUR, SensorDeviceClass.ENERGY),
        ("kWh", UnitOfEnergy.KILO_WATT_HOUR, SensorDeviceClass.ENERGY),
        ("l", UnitOfVolume.LITERS, SensorDeviceClass.WATER),
        ("m3", UnitOfVolume.CUBIC_METERS, SensorDeviceClass.WATER),
        ("m³", UnitOfVolume.CUBIC_METERS, SensorDeviceClass.WATER),
    ):
        entity = _make_entity(coordinator, replace(mock_meter, unit=raw_unit))
        assert entity.native_unit_of_measurement == expected_unit
        assert entity.device_class == expected_class

    # An unrecognised unit is passed through, but must not claim a device
    # class HA would then reject.
    entity = _make_entity(coordinator, replace(mock_meter, unit="widgets"))
    assert entity.native_unit_of_measurement == "widgets"
    assert entity.device_class is None


async def test_sensor_allocator_unit_is_passed_through_verbatim(mock_meter):
    """Heat cost allocators report "units", which has no Home Assistant
    equivalent and is passed straight through.

    The casing must be preserved exactly as Brunata sends it. Normalising it
    would change native_unit_of_measurement on existing entities, which Home
    Assistant treats as a unit change on a TOTAL_INCREASING sensor and which
    forces users to migrate or discard their long term statistics."""
    coordinator = MagicMock()
    coordinator.data = {"12345": mock_meter}
    mock_meter = replace(mock_meter, meter_type="Radiator")

    for raw_unit in ("units", "Units"):
        entity = _make_entity(coordinator, replace(mock_meter, unit=raw_unit))
        assert entity.native_unit_of_measurement == raw_unit
        assert entity.device_class is None

    # Only a genuinely absent meterUnit falls back to the default.
    for missing in ("", "   ", None):
        entity = _make_entity(coordinator, replace(mock_meter, unit=missing or ""))
        assert entity.native_unit_of_measurement == FALLBACK_UNIT
        assert entity.device_class is None


async def test_sensor_display_precision_by_unit(mock_meter):
    """Heat cost allocators show whole numbers — the API's .00 decimals carry
    no meaningful precision. Water keeps 3 decimals, energy keeps 2. This is
    display-only: native_value is untouched either way."""
    coordinator = MagicMock()
    coordinator.data = {"12345": mock_meter}

    for meter_type, raw_unit, expected_precision in (
        ("Radiator", "units", 0),
        ("Water", "m3", 3),
        ("Water", "l", 3),
        ("Energy", "kWh", 2),
    ):
        entity = _make_entity(
            coordinator,
            replace(mock_meter, meter_type=meter_type, unit=raw_unit),
        )
        assert entity.suggested_display_precision == expected_precision


async def test_sensor_reset_detection(mock_meter):
    """Heat cost allocators are zeroed on 1 January, so a decrease at the turn
    of the year is accepted straight away. A single mid-year decrease is not —
    it has to be confirmed by later readings first."""
    mock_meter = replace(mock_meter, meter_type="Radiator", unit="units")

    coordinator = MagicMock()
    coordinator.data = {"12345": mock_meter}
    entity = _make_entity(coordinator, mock_meter)

    # Last reading of the accounting year.
    coordinator.data = {"12345": replace(mock_meter, value=4820.0, reading_date=date(2024, 12, 20))}
    entity._apply_latest_reading()
    assert entity.native_value == 4820.0

    # Decrease outside the reset window — must be rejected as a glitch.
    coordinator.data = {"12345": replace(mock_meter, value=10.0, reading_date=date(2024, 6, 15))}
    entity._apply_latest_reading()
    assert entity.native_value == 4820.0

    # The 1 January reset — must be accepted.
    coordinator.data = {"12345": replace(mock_meter, value=0.0, reading_date=date(2025, 1, 1))}
    entity._apply_latest_reading()
    assert entity.native_value == 0.0
    assert entity.extra_state_attributes["reading_date"] == "2025-01-01"


async def test_sensor_reset_accepted_when_first_reading_arrives_late(mock_meter):
    """Allocators report infrequently, so the first reading after the 1 January
    reset is not necessarily dated 1 January. Matching only 31 Dec / 1 Jan
    rejected those, and since the cached value is never lowered the sensor then
    stayed frozen at the pre-reset value for the rest of the year."""
    mock_meter = replace(mock_meter, meter_type="Radiator", unit="units")

    coordinator = MagicMock()
    coordinator.data = {"12345": mock_meter}

    # Mid-January: inside the December/January window.
    entity = _make_entity(coordinator, mock_meter)
    coordinator.data = {"12345": replace(mock_meter, value=4820.0, reading_date=date(2024, 12, 20))}
    entity._apply_latest_reading()
    coordinator.data = {"12345": replace(mock_meter, value=11.0, reading_date=date(2025, 1, 17))}
    entity._apply_latest_reading()
    assert entity.native_value == 11.0

    # February: outside the window, but the calendar year has advanced since
    # the last accepted reading, which is the reliable signal.
    entity = _make_entity(coordinator, mock_meter)
    coordinator.data = {"12345": replace(mock_meter, value=4820.0, reading_date=date(2024, 12, 20))}
    entity._apply_latest_reading()
    coordinator.data = {"12345": replace(mock_meter, value=11.0, reading_date=date(2025, 2, 3))}
    entity._apply_latest_reading()
    assert entity.native_value == 11.0


async def test_sensor_isolated_decrease_rejected_as_glitch(mock_meter):
    """A one-off decrease is an API glitch: the next reading is back where it
    belongs. Accepting it under TOTAL_INCREASING would record a false spike on
    the way back up."""
    mock_meter = replace(mock_meter, meter_type="Water", unit="m3")

    coordinator = MagicMock()
    coordinator.data = {"12345": mock_meter}
    entity = _make_entity(coordinator, mock_meter)

    coordinator.data = {"12345": replace(mock_meter, value=312.5, reading_date=date(2025, 1, 2))}
    entity._apply_latest_reading()
    assert entity.native_value == 312.5

    coordinator.data = {"12345": replace(mock_meter, value=0.0, reading_date=date(2025, 1, 3))}
    entity._apply_latest_reading()
    assert entity.native_value == 312.5

    coordinator.data = {"12345": replace(mock_meter, value=313.0, reading_date=date(2025, 1, 4))}
    entity._apply_latest_reading()
    assert entity.native_value == 313.0

    # The glitch must not count towards a later confirmation.
    assert entity._pending_reset_dates == set()


async def test_sensor_accepts_reset_when_meter_number_changes(mock_meter):
    """A replaced meter starts over from zero. The meter number identifies the
    physical device, so a change is proof enough on its own — any meter type,
    any time of year."""
    mock_meter = replace(mock_meter, meter_type="Water", unit="m3", meter_no="M12345")

    coordinator = MagicMock()
    coordinator.data = {"12345": mock_meter}
    entity = _make_entity(coordinator, mock_meter)

    coordinator.data = {"12345": replace(mock_meter, value=312.5, reading_date=date(2025, 6, 1))}
    entity._apply_latest_reading()
    assert entity.native_value == 312.5

    # Same meter_id, new physical meter, mid-year.
    coordinator.data = {
        "12345": replace(
            mock_meter, meter_no="M99999", value=0.4, reading_date=date(2025, 6, 20)
        )
    }
    entity._apply_latest_reading()
    assert entity.native_value == 0.4
    assert entity._meter_no == "M99999"


async def test_sensor_accepts_reset_confirmed_across_reading_dates(mock_meter):
    """When a meter is replaced but Brunata keeps the old meter number, the
    only signal left is that the decrease persists — the new device counts up
    from zero, so every reading stays below the old value."""
    mock_meter = replace(mock_meter, meter_type="Water", unit="m3")

    coordinator = MagicMock()
    coordinator.data = {"12345": mock_meter}
    entity = _make_entity(coordinator, mock_meter)

    coordinator.data = {"12345": replace(mock_meter, value=312.5, reading_date=date(2025, 6, 1))}
    entity._apply_latest_reading()
    assert entity.native_value == 312.5

    # Two readings below the cached value are not yet enough.
    for day, value in ((20, 0.0), (21, 0.3)):
        coordinator.data = {
            "12345": replace(mock_meter, value=value, reading_date=date(2025, 6, day))
        }
        entity._apply_latest_reading()
        assert entity.native_value == 312.5

    # The third distinct reading date confirms it.
    coordinator.data = {"12345": replace(mock_meter, value=0.7, reading_date=date(2025, 6, 22))}
    entity._apply_latest_reading()
    assert entity.native_value == 0.7
    assert entity.extra_state_attributes["reading_date"] == "2025-06-22"
    assert entity._pending_reset_dates == set()


async def test_sensor_confirmation_counts_dates_not_updates(mock_meter):
    """The coordinator polls far more often than meters report, so the same
    reading is re-served many times. Counting updates instead of reading dates
    would let a single glitch confirm itself within the hour."""
    mock_meter = replace(mock_meter, meter_type="Water", unit="m3")

    coordinator = MagicMock()
    coordinator.data = {"12345": mock_meter}
    entity = _make_entity(coordinator, mock_meter)

    coordinator.data = {"12345": replace(mock_meter, value=312.5, reading_date=date(2025, 6, 1))}
    entity._apply_latest_reading()

    coordinator.data = {"12345": replace(mock_meter, value=0.0, reading_date=date(2025, 6, 20))}
    for _ in range(5):
        entity._apply_latest_reading()
    assert entity.native_value == 312.5


async def test_sensor_decrease_without_reading_date_is_ignored(mock_meter):
    """A reading can arrive without a parseable readingDate. It can neither be
    placed in the calendar year nor contribute to a confirmation, so it must be
    dropped — not crash the coordinator callback."""
    mock_meter = replace(mock_meter, meter_type="Radiator", unit="units")

    coordinator = MagicMock()
    coordinator.data = {"12345": mock_meter}
    entity = _make_entity(coordinator, mock_meter)

    coordinator.data = {"12345": replace(mock_meter, value=4820.0, reading_date=date(2024, 12, 20))}
    entity._apply_latest_reading()

    coordinator.data = {"12345": replace(mock_meter, value=0.0, reading_date=None)}
    entity._apply_latest_reading()
    assert entity.native_value == 4820.0


async def test_sensor_restores_last_state_before_coordinator_has_data(
    hass: HomeAssistant, mock_brunata_client, mock_meter
):
    """A restarted/reloaded sensor must restore its last reading and stay
    available even if the coordinator has not delivered a fresh reading yet —
    this is the exact gap async_added_to_hass()'s restore closes."""
    # has_entity_name + the device name determine the generated entity_id
    # (it includes the meter type, e.g. "heat"). Dropping the "Brunata"
    # prefix from the device name changes the slug it's derived from — this
    # value needs re-confirming against the actual HA-registered entity_id
    # in a real test run, same as when it was first added.
    entity_id = "sensor.heat_12345_consumption"
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

    # No fresh reading available yet at startup, so only the restored state
    # can give the entity a value.
    mock_brunata_client.async_get_meters = AsyncMock(
        return_value={"12345": replace(mock_meter, value=None, reading_date=None)}
    )

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
    coordinator.data = {"12345": replace(mock_meter, value=None, reading_date=None)}

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


# --- placement -----------------------------------------------------------
#
# placement is the customer-assigned location label from Brunata's own UI
# (e.g. "Bathroom (Cold)"), fetched separately from the reading itself and
# absent whenever that fetch failed or the meter has none set.

async def test_sensor_device_name_uses_placement_when_present(mock_meter):
    """When a placement is available, the device name leads with it, so the
    device is recognisable without opening it."""
    meter = replace(mock_meter, placement="Bathroom (Cold)")
    coordinator = MagicMock()
    coordinator.data = {"12345": meter}
    entity = _make_entity(coordinator, meter)

    assert entity._attr_device_info["name"] == "Heat - Bathroom (Cold)"


async def test_sensor_device_name_falls_back_without_placement(mock_meter):
    """No placement (fetch failed, or none set in Brunata) keeps the original
    type+ID name rather than showing something blank."""
    coordinator = MagicMock()
    coordinator.data = {"12345": mock_meter}
    entity = _make_entity(coordinator, mock_meter)

    assert entity._attr_device_info["name"] == "Heat (12345)"


async def test_sensor_extra_state_attributes_include_placement_when_set(mock_meter):
    meter = replace(mock_meter, placement="Living room")
    coordinator = MagicMock()
    coordinator.data = {"12345": meter}
    entity = _make_entity(coordinator, meter)

    assert entity.extra_state_attributes["placement"] == "Living room"


async def test_sensor_extra_state_attributes_omit_placement_when_absent(mock_meter):
    coordinator = MagicMock()
    coordinator.data = {"12345": mock_meter}
    entity = _make_entity(coordinator, mock_meter)

    assert "placement" not in entity.extra_state_attributes


async def test_sensor_placement_follows_later_updates(mock_meter):
    """Relabelling a meter in Brunata's UI has to reach the attribute.

    placement is read from the coordinator on every poll, so it must be
    refreshed in _apply_latest_reading() and not only captured in __init__ —
    otherwise a renamed meter keeps its old label until the entry is reloaded.
    """
    meter = replace(mock_meter, placement="Living room")
    coordinator = MagicMock()
    coordinator.data = {"12345": meter}
    entity = _make_entity(coordinator, meter)
    assert entity.extra_state_attributes["placement"] == "Living room"

    coordinator.data = {"12345": replace(meter, value=200.0, placement="Kitchen")}
    entity._apply_latest_reading()

    assert entity.extra_state_attributes["placement"] == "Kitchen"


async def test_sensor_placement_updates_even_when_the_reading_is_rejected(mock_meter):
    """A held-back reading must not hold back the label.

    placement is metadata, not part of the reading, so it is applied before the
    accept/reject decision. A meter mid-way through reset confirmation still
    shows its current label while its value is deliberately frozen.
    """
    meter = replace(mock_meter, value=100.0, placement="Living room")
    coordinator = MagicMock()
    coordinator.data = {"12345": meter}
    entity = _make_entity(coordinator, meter)
    entity._apply_latest_reading()
    assert entity.native_value == 100.0

    # An unconfirmed mid-year decrease: the value is rejected, the label is not.
    coordinator.data = {
        "12345": replace(
            meter, value=1.0, reading_date=date(2024, 6, 15), placement="Kitchen"
        )
    }
    entity._apply_latest_reading()

    assert entity.native_value == 100.0
    assert entity.extra_state_attributes["placement"] == "Kitchen"


async def test_sensor_placement_is_cleared_when_it_disappears(mock_meter):
    """A failed placements fetch degrades to None for every meter. The attribute
    should follow rather than serve a label the API no longer reports."""
    meter = replace(mock_meter, placement="Living room")
    coordinator = MagicMock()
    coordinator.data = {"12345": meter}
    entity = _make_entity(coordinator, meter)

    coordinator.data = {"12345": replace(meter, value=200.0, placement=None)}
    entity._apply_latest_reading()

    assert "placement" not in entity.extra_state_attributes

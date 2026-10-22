"""Test Brunata sensor."""

import logging
from dataclasses import replace
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.components.sensor import (
    DOMAIN as SENSOR_DOMAIN,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, mock_restore_cache

from homeassistant.util import dt as dt_util

from custom_components.brunata.api import (
    BASE_URL,
    SUPPORTED_METER_TYPES,
    BrunataConnectionError,
    ParseReport,
)
from custom_components.brunata.const import DOMAIN
from custom_components.brunata.sensor import (
    ANNUAL_RESET_METER_TYPES,
    BrunataRestoredData,
    BrunataSensor,
    _consumption_shows_reset,
    _is_cumulative_unit,
)


def _make_entity(coordinator, meter):
    """Build a BrunataSensor without going through CoordinatorEntity.__init__."""
    with patch(
        "homeassistant.helpers.update_coordinator.CoordinatorEntity.__init__",
        return_value=None,
    ):
        entity = BrunataSensor(coordinator, meter)
    entity.coordinator = coordinator
    return entity


async def _restore(entity, last_state, last_extra=None):
    """Run async_added_to_hass() against a given previous state.

    Both halves of the restore are patched: the state carries the value and the
    reading date, the extra data carries the meter number and mounting date the
    decrease guard compares against.
    """
    with patch(
        "homeassistant.helpers.update_coordinator.CoordinatorEntity.async_added_to_hass",
        AsyncMock(),
    ), patch.object(
        entity, "async_get_last_state", AsyncMock(return_value=last_state)
    ), patch.object(
        entity, "async_get_last_extra_data", AsyncMock(return_value=last_extra)
    ):
        await entity.async_added_to_hass()


async def test_a_reported_meter_becomes_a_consumption_sensor(
    hass: HomeAssistant, mock_brunata_client, mock_meter
):
    """Setting the entry up turns the one meter Brunata reports into an entity.

    Found by unique id through the entity registry, the same way
    _setup_reading_time() below finds its own entity, rather than by scanning
    the state machine for an entity id that ends in "_consumption". The entity
    id follows the friendly name and moves whenever the meter's placement label
    changes in Brunata Online; the unique id is the string Home Assistant hangs
    the entity's long term statistics on and must never move. Asserting on the
    one that is allowed to change is how a renamed meter would fail this test
    for no reason — or, worse, pass it by matching some other entity.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "test@example.com", "password": "password123"},
    )
    entry.add_to_hass(hass)
    mock_brunata_client.async_get_meters = AsyncMock(
        return_value={"12345": mock_meter}
    )

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    entity_id = er.async_get(hass).async_get_entity_id(
        SENSOR_DOMAIN, DOMAIN, "brunata_12345_consumption"
    )
    assert entity_id is not None

    state = hass.states.get(entity_id)
    assert state.attributes["friendly_name"] == "Water (12345) Consumption"
    # Always an ISO string, whether the value came from the API or from a
    # restored state after a restart.
    assert state.attributes["reading_date"] == "2024-01-01"


# --- latest reading time (issue #89) ---------------------------------------

_READ_AT = datetime(2026, 10, 10, 23, 50, tzinfo=UTC)


async def _setup_reading_time(hass, mock_brunata_client, meter):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "test@example.com", "password": "password123"},
    )
    entry.add_to_hass(hass)
    mock_brunata_client.async_get_meters = AsyncMock(return_value={"12345": meter})
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        SENSOR_DOMAIN, DOMAIN, "brunata_12345_reading_time"
    )
    assert entity_id is not None
    return entry, registry, entity_id


async def test_each_meter_gets_a_reading_time_sensor(
    hass: HomeAssistant, mock_brunata_client, mock_meter
):
    """The full timestamp, as a diagnostic entity on the meter's own device,
    next to the consumption sensor rather than instead of its attribute."""
    meter = replace(mock_meter, reading_time=_READ_AT)
    _, registry, entity_id = await _setup_reading_time(
        hass, mock_brunata_client, meter
    )

    state = hass.states.get(entity_id)
    assert state.state == "2026-10-10T23:50:00+00:00"
    assert state.attributes["device_class"] == SensorDeviceClass.TIMESTAMP
    assert state.attributes["friendly_name"] == "Water (12345) Latest reading"

    entry = registry.async_get(entity_id)
    assert entry.entity_category is EntityCategory.DIAGNOSTIC
    # Enabled from the start.
    assert entry.disabled_by is None

    consumption_id = registry.async_get_entity_id(
        SENSOR_DOMAIN, DOMAIN, "brunata_12345_consumption"
    )
    assert registry.async_get(consumption_id).device_id == entry.device_id
    # The day-only attribute is unchanged.
    assert hass.states.get(consumption_id).attributes["reading_date"] == "2024-01-01"


async def test_the_reading_time_follows_new_readings_and_keeps_the_last(
    hass: HomeAssistant, mock_brunata_client, mock_meter
):
    meter = replace(mock_meter, reading_time=_READ_AT)
    entry, _, entity_id = await _setup_reading_time(
        hass, mock_brunata_client, meter
    )
    coordinator = entry.runtime_data

    later = datetime(2026, 10, 11, 1, 53, tzinfo=UTC)
    mock_brunata_client.async_get_meters = AsyncMock(
        return_value={"12345": replace(meter, reading_time=later)}
    )
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "2026-10-11T01:53:00+00:00"

    # A poll without a usable time leaves the last one in place.
    mock_brunata_client.async_get_meters = AsyncMock(
        return_value={"12345": replace(meter, reading_time=None)}
    )
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "2026-10-11T01:53:00+00:00"


async def test_the_reading_time_goes_unavailable_with_its_meter(
    hass: HomeAssistant, mock_brunata_client, mock_meter
):
    """Both sensors on a meter go unavailable together, for the same reason."""
    meter = replace(mock_meter, reading_time=_READ_AT)
    entry, _, entity_id = await _setup_reading_time(
        hass, mock_brunata_client, meter
    )

    mock_brunata_client.async_get_meters = AsyncMock(return_value={})
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get(entity_id).state == "unavailable"


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
    mock_meter = replace(mock_meter, meter_type="Radiator", meter_type_code=1)

    for raw_unit in ("units", "Units"):
        entity = _make_entity(coordinator, replace(mock_meter, unit=raw_unit))
        assert entity.native_unit_of_measurement == raw_unit
        assert entity.device_class is None

    # A meter with no usable unit no longer reaches this module at all: api/parse.py
    # drops it rather than filling one in. See
    # test_a_meter_whose_unit_cannot_be_resolved_is_skipped in test_api.py.


async def test_sensor_display_precision_comes_from_the_api(mock_meter):
    """Brunata states a precision per meter, and outside heat cost allocators
    the sensor uses it — 3 for water matches the three decimals in the reading.
    Allocators are covered by test_heat_cost_allocators_show_one_decimal."""
    coordinator = MagicMock()
    coordinator.data = {"12345": mock_meter}

    # The meter type is not part of this: only the unit and Brunata's own
    # decimals reach suggested_display_precision. kWh is here because UNIT_MAP
    # can express it, not because any meter type reaching this code reports it.
    for raw_unit, decimals in (("m3", 3), ("kWh", 2)):
        entity = _make_entity(
            coordinator,
            replace(mock_meter, unit=raw_unit, decimals=decimals),
        )
        assert entity.suggested_display_precision == decimals


async def test_sensor_display_precision_falls_back_to_the_unit(mock_meter):
    """decimals is optional. Without it the unit decides, so a meter Brunata
    describes incompletely still gets a sensible number of digits rather than
    none at all.

    This is display-only either way: native_value and Long Term Statistics keep
    the full float."""
    coordinator = MagicMock()
    coordinator.data = {"12345": mock_meter}

    # Dal is in Brunata's table and in no UNIT_MAP entry, so it reaches the
    # last branch. Allocator units never do: they are decided before decimals.
    for raw_unit, expected_precision in (
        ("Dal", 0),
        ("m3", 3),
        ("l", 3),
        ("kWh", 2),
    ):
        entity = _make_entity(
            coordinator,
            replace(mock_meter, unit=raw_unit, decimals=None),
        )
        assert entity.suggested_display_precision == expected_precision


@pytest.mark.parametrize("decimals", [0, None])
@pytest.mark.parametrize("raw_unit", ["units", "Doprimo units"])
async def test_heat_cost_allocators_show_one_decimal(mock_meter, raw_unit, decimals):
    """Brunata sends decimals 0 for heat cost allocators, but the reading
    carries one decimal (110.9), so allocator units always show one.

    decimals 0 is what Brunata sends; None is the case where it sends nothing.
    Doprimo units stands for the vendor-specific spellings, which carry the
    same marker as the plain units.
    """
    coordinator = MagicMock()
    coordinator.data = {"12345": mock_meter}

    entity = _make_entity(
        coordinator,
        replace(mock_meter, unit=raw_unit, decimals=decimals, value=110.9),
    )

    assert entity.suggested_display_precision == 1


@pytest.mark.parametrize(
    ("raw_unit", "expected_unit", "expected_class"),
    [
        # Every consumption unit in Brunata's live measurementUnit table that
        # Home Assistant has a constant for. Spelled exactly as Brunata does.
        ("m³", UnitOfVolume.CUBIC_METERS, SensorDeviceClass.WATER),
        ("liter", UnitOfVolume.LITERS, SensorDeviceClass.WATER),
        ("Wh", UnitOfEnergy.WATT_HOUR, SensorDeviceClass.ENERGY),
        ("kWh", UnitOfEnergy.KILO_WATT_HOUR, SensorDeviceClass.ENERGY),
        ("MWh", UnitOfEnergy.MEGA_WATT_HOUR, SensorDeviceClass.ENERGY),
        ("J", UnitOfEnergy.JOULE, SensorDeviceClass.ENERGY),
        ("kJ", UnitOfEnergy.KILO_JOULE, SensorDeviceClass.ENERGY),
        ("MJ", UnitOfEnergy.MEGA_JOULE, SensorDeviceClass.ENERGY),
        ("GJ", UnitOfEnergy.GIGA_JOULE, SensorDeviceClass.ENERGY),
        ("Kcal", UnitOfEnergy.KILO_CALORIE, SensorDeviceClass.ENERGY),
        ("Mcal", UnitOfEnergy.MEGA_CALORIE, SensorDeviceClass.ENERGY),
        ("GCal", UnitOfEnergy.GIGA_CALORIE, SensorDeviceClass.ENERGY),
    ],
)
async def test_sensor_maps_the_units_brunata_actually_reports(
    mock_meter, raw_unit, expected_unit, expected_class
):
    """Sending Brunata's own spelling straight to Home Assistant would give an
    invalid unit for the device class, and the sensor's Long Term Statistics
    would be discarded — which is exactly what happened to the water meters."""
    coordinator = MagicMock()
    coordinator.data = {"12345": mock_meter}

    entity = _make_entity(coordinator, replace(mock_meter, unit=raw_unit))
    assert entity.native_unit_of_measurement == expected_unit
    assert entity.device_class == expected_class


@pytest.mark.parametrize(
    "raw_unit", ["Btu", "Dal", "Doprimo units", "m³ per hour"]
)
async def test_sensor_unmappable_units_claim_no_device_class(mock_meter, raw_unit):
    """Anything outside the supported water/energy/allocator units passes
    through without a device class. Claiming one Home Assistant would reject
    is worse than claiming none."""
    entity = _make_entity(MagicMock(), replace(mock_meter, unit=raw_unit))

    assert entity.native_unit_of_measurement == raw_unit
    assert entity.device_class is None


async def test_sensor_reset_detection(mock_meter):
    """Heat cost allocators are zeroed on 1 January, so a decrease across the
    turn of the year is accepted. A mid-year decrease is not: with no
    replacement to point at, it is a glitch, and the cached value stands."""
    mock_meter = replace(mock_meter, meter_type="Radiator", meter_type_code=1, unit="units")

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
    """The first reading after the 1 January reset is not necessarily dated
    1 January. Matching only 31 Dec / 1 Jan rejected those, and since the
    cached value is never lowered the sensor then stayed frozen at the
    pre-reset value for the rest of the year.

    How often Brunata publishes a reading is set by its metering hardware and
    varies with consumption. The rule under test does not depend on that — it
    compares calendar years, not intervals — which is why a mid-January date is
    accepted here.
    """
    mock_meter = replace(mock_meter, meter_type="Radiator", meter_type_code=1, unit="units")

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


async def test_january_glitch_within_the_same_year_is_rejected(mock_meter):
    """The month window used to be checked even when the previous reading date
    was known, which made it wider than intended: a fall on 20 January, with
    the last accepted reading dated 12 January of the *same* year, was adopted
    as an annual reset even though no year boundary had been crossed. The
    calendar year is the signal; the window is only a stand-in for not having
    one."""
    meter = replace(mock_meter, meter_type="Radiator", meter_type_code=1, unit="units")

    coordinator = MagicMock()
    coordinator.data = {"12345": meter}
    entity = _make_entity(coordinator, meter)

    coordinator.data = {
        "12345": replace(meter, value=140.0, reading_date=date(2026, 1, 12))
    }
    entity._apply_latest_reading()
    assert entity.native_value == 140.0

    coordinator.data = {
        "12345": replace(meter, value=0.0, reading_date=date(2026, 1, 20))
    }
    entity._apply_latest_reading()
    assert entity.native_value == 140.0


async def test_january_window_still_applies_without_a_previous_date(mock_meter):
    """The fallback has to keep working. A state restored from before the
    reading date was recorded has no previous date to compare against, and
    that is exactly the case the window exists for.

    17 January is outside the year rule — there is no previous year to be
    later than — so accepting it can only be the window. Remove the window and
    this reading is rejected and the sensor stays on 4820.
    """
    meter = replace(mock_meter, meter_type="Radiator", meter_type_code=1, unit="units")

    coordinator = MagicMock()
    coordinator.data = {
        "12345": replace(meter, value=11.0, reading_date=date(2026, 1, 17))
    }
    entity = _make_entity(coordinator, meter)

    # No reading_date attribute, so nothing seeds the baseline.
    await _restore(entity, MagicMock(state="4820.0", attributes={}), None)

    assert entity.native_value == 11.0
    # And the accepted reading becomes the baseline, so the window is not
    # consulted again next time.
    assert entity._last_reading_day == date(2026, 1, 17)


async def test_an_undated_reading_does_not_reopen_the_january_window(mock_meter):
    """An accepted reading without a date must not clear the year baseline.

    _is_annual_reset() reads a missing _last_reading_day as "no baseline" and
    falls back to the December/January window — the wider rule the calendar-year
    comparison replaced. Overwriting the baseline with None on every accepted
    reading meant one undated reading reopened that window for the rest of the
    December and January it fell in, and a glitch dated 31 December was then
    adopted as an annual reset.

    The control below is the point of the test: the same glitch, without the
    undated reading in front of it, is rejected.
    """
    meter = replace(mock_meter, meter_type="Radiator", meter_type_code=1, unit="units")

    coordinator = MagicMock()
    coordinator.data = {"12345": meter}
    entity = _make_entity(coordinator, meter)

    coordinator.data = {
        "12345": replace(meter, value=4820.0, reading_date=date(2025, 12, 20))
    }
    entity._apply_latest_reading()
    assert entity._last_reading_day == date(2025, 12, 20)

    # A reading Brunata sent with no usable readingDate. The value is fresher,
    # so it is taken; the baseline is not something it can speak to.
    coordinator.data = {
        "12345": replace(meter, value=4830.0, reading_date=None)
    }
    entity._apply_latest_reading()
    assert entity.native_value == 4830.0
    assert entity._last_reading_day == date(2025, 12, 20)
    assert entity.extra_state_attributes["reading_date"] == "2025-12-20"

    # 31 December of the same year: inside the fallback window, but no year
    # boundary has been crossed, so it is a glitch.
    coordinator.data = {
        "12345": replace(meter, value=3.0, reading_date=date(2025, 12, 31))
    }
    entity._apply_latest_reading()
    assert entity.native_value == 4830.0

    # And the year rule still works afterwards.
    coordinator.data = {
        "12345": replace(meter, value=3.0, reading_date=date(2026, 1, 4))
    }
    entity._apply_latest_reading()
    assert entity.native_value == 3.0


async def test_every_annually_reset_type_is_a_supported_type():
    """The two sets number the same thing and live in different files.

    ANNUAL_RESET_METER_TYPES is read off meter_type_code, which _parse_meters()
    only ever sets for a code that passed SUPPORTED_METER_TYPES. A type in the
    reset set but not the allowlist would therefore be a rule that can never
    fire — dead, and misleading about which meters this integration handles.
    """
    assert ANNUAL_RESET_METER_TYPES <= SUPPORTED_METER_TYPES


# Every energy unit in Brunata's measurementUnit table, spelled exactly as the
# table spells it. Issue #93 did not show which one a heat meter reports — the
# allowlist dropped the meter before its unit was read — so all of them are
# checked rather than one being picked.
_BRUNATA_ENERGY_UNITS = (
    ("Wh", UnitOfEnergy.WATT_HOUR),
    ("kWh", UnitOfEnergy.KILO_WATT_HOUR),
    ("MWh", UnitOfEnergy.MEGA_WATT_HOUR),
    ("J", UnitOfEnergy.JOULE),
    ("kJ", UnitOfEnergy.KILO_JOULE),
    ("MJ", UnitOfEnergy.MEGA_JOULE),
    ("GJ", UnitOfEnergy.GIGA_JOULE),
    ("Kcal", UnitOfEnergy.KILO_CALORIE),
    ("Mcal", UnitOfEnergy.MEGA_CALORIE),
    ("GCal", UnitOfEnergy.GIGA_CALORIE),
)


@pytest.mark.parametrize(("raw_unit", "expected_unit"), _BRUNATA_ENERGY_UNITS)
async def test_heat_meter_in_an_energy_unit_is_an_energy_sensor(
    mock_meter, raw_unit, expected_unit
):
    """A heat meter (meterType 6, issue #93) in any energy unit Brunata can
    send becomes an energy sensor that only counts up.

    It is not reset once a year the way a heat cost allocator is: like water
    and electricity it starts from zero only when the meter is replaced, which
    _accept_reading() recognises on its own.
    """
    meter = replace(
        mock_meter,
        meter_type="Energy",
        meter_type_code=6,
        unit=raw_unit,
        decimals=None,
    )
    entity = _make_entity(MagicMock(), meter)

    assert entity.native_unit_of_measurement == expected_unit
    assert entity.device_class == SensorDeviceClass.ENERGY
    assert entity.state_class == SensorStateClass.TOTAL_INCREASING
    assert entity.suggested_display_precision == 2
    assert entity._resets_annually is False


async def test_heat_meter_in_btu_gets_the_cautious_default(mock_meter):
    """Btu is the one energy unit in Brunata's table that Home Assistant has
    no constant for. A heat meter reporting it gets the same treatment as any
    unrecognised unit: no device class, and not summed into statistics. See
    _is_cumulative_unit()."""
    meter = replace(mock_meter, meter_type="Energy", meter_type_code=6, unit="Btu")
    entity = _make_entity(MagicMock(), meter)

    assert entity.native_unit_of_measurement == "Btu"
    assert entity.device_class is None
    assert entity.state_class == SensorStateClass.MEASUREMENT


async def test_january_window_is_not_consulted_once_a_date_is_known(mock_meter):
    """The other half: with a baseline in hand, only the year rule applies.

    Asserted on _is_annual_reset() directly, because _apply_latest_reading()
    overwrites the baseline as soon as it accepts something — which is what
    made the first version of the test above assert on the wrong moment.
    """
    meter = replace(mock_meter, meter_type="Radiator", meter_type_code=1, unit="units")
    entity = _make_entity(MagicMock(), meter)

    entity._last_reading_day = None
    assert entity._is_annual_reset(date(2026, 1, 17)) is True
    assert entity._is_annual_reset(date(2026, 6, 14)) is False

    entity._last_reading_day = date(2026, 1, 12)
    assert entity._is_annual_reset(date(2026, 1, 17)) is False
    assert entity._is_annual_reset(date(2027, 1, 17)) is True


@pytest.mark.parametrize(
    "meter_type", ["Radiator", "Heat cost allocator", "Varmefordelingsmåler", "1"]
)
async def test_annual_reset_follows_the_code_not_the_resolved_name(
    mock_meter, meter_type
):
    """meterType 1 is zeroed on 1 January whatever the lookup table calls it.

    The rule used to match the substring "radiator" in the resolved name. That
    name is a translation Brunata owns, fetched in the language api/const.py's LOCALE
    asks for — so relabelling the entry, or ever changing the locale, would
    have turned the rule off with nothing failing. And because the cached value
    is never lowered, the rejected 1 January decrease would take every reading
    for the rest of the year down with it: the sensor freezes at the pre-reset
    value until the new period passes it, which for an allocator is a year of
    wrong data.

    "1" is in this list because it is what _lookup() falls back to when the
    meter type code does not resolve at all. The unit gets no such fallback.
    """
    meter = replace(
        mock_meter, meter_type=meter_type, meter_type_code=1, unit="units"
    )

    coordinator = MagicMock()
    coordinator.data = {"12345": meter}
    entity = _make_entity(coordinator, meter)
    assert entity._resets_annually is True

    coordinator.data = {
        "12345": replace(meter, value=4820.0, reading_date=date(2025, 12, 20))
    }
    entity._apply_latest_reading()

    coordinator.data = {
        "12345": replace(meter, value=3.0, reading_date=date(2026, 1, 3))
    }
    entity._apply_latest_reading()

    assert entity.native_value == 3.0


async def test_a_water_meter_named_radiator_does_not_reset_annually(mock_meter):
    """The other half of the same rule.

    Matching on the name meant any meter whose label happened to contain
    "radiator" inherited the new year exemption. The code decides, so a water
    meter keeps its guard no matter what the table calls it.
    """
    meter = replace(
        mock_meter, meter_type="Radiator", meter_type_code=2, unit="m3"
    )

    coordinator = MagicMock()
    coordinator.data = {"12345": meter}
    entity = _make_entity(coordinator, meter)
    assert entity._resets_annually is False

    coordinator.data = {
        "12345": replace(meter, value=312.5, reading_date=date(2025, 12, 20))
    }
    entity._apply_latest_reading()

    coordinator.data = {
        "12345": replace(meter, value=0.4, reading_date=date(2026, 1, 3))
    }
    entity._apply_latest_reading()

    assert entity.native_value == 312.5


async def test_an_unidentified_meter_type_does_not_reset_annually(mock_meter):
    """meter_type_code is None when the payload's meterType could not be read.

    Nothing with an unreadable type reaches the sensor today — the allowlist
    drops it in _parse_meters() — but the rule must fail closed anyway, the
    same way SUPPORTED_METER_TYPES does with the same value.
    """
    meter = replace(
        mock_meter, meter_type="Radiator", meter_type_code=None, unit="units"
    )
    entity = _make_entity(MagicMock(), meter)

    assert entity._resets_annually is False


async def test_sensor_isolated_decrease_rejected_as_glitch(mock_meter):
    """A one-off decrease is an API glitch: the next reading is back where it
    belongs. Accepting it under TOTAL_INCREASING would record a false spike on
    the way back up."""
    mock_meter = replace(mock_meter, meter_type="Water", meter_type_code=2, unit="m3")

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

    # The glitch left no trace that could later be mistaken for a reset.
    assert entity.native_value == 313.0


async def test_sensor_accepts_reset_when_meter_number_changes(mock_meter):
    """A replaced meter starts over from zero. The meter number identifies the
    physical device, so a change is proof enough on its own — any meter type,
    any time of year."""
    mock_meter = replace(
        mock_meter, meter_type="Water", meter_type_code=2, unit="m3", meter_no="M12345"
    )

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


async def test_sensor_accepts_reset_when_mounting_date_changes(mock_meter):
    """Brunata states when a meter was installed. A new mounting date is a
    replacement reported as fact, so the decrease needs no corroboration —
    even if the meter number were somehow reused."""
    mock_meter = replace(mock_meter, meter_type="Water", meter_type_code=2, unit="m3")

    coordinator = MagicMock()
    coordinator.data = {"12345": mock_meter}
    entity = _make_entity(coordinator, mock_meter)

    coordinator.data = {
        "12345": replace(mock_meter, value=312.5, reading_date=date(2025, 6, 1))
    }
    entity._apply_latest_reading()
    assert entity.native_value == 312.5

    replaced = replace(
        mock_meter,
        value=0.4,
        reading_date=date(2025, 6, 20),
        mounting_date=datetime(2025, 6, 19, 9, 0, tzinfo=UTC),
    )
    coordinator.data = {"12345": replaced}
    entity._apply_latest_reading()

    assert entity.native_value == 0.4
    assert entity._mounting_date == replaced.mounting_date


async def test_sensor_unexplained_decrease_is_rejected(mock_meter):
    """Neither a replacement nor an annual reset. Adopting it under
    TOTAL_INCREASING would record a false consumption spike on the way back
    up, so the cached value stands and a warning is logged instead."""
    mock_meter = replace(mock_meter, meter_type="Water", meter_type_code=2, unit="m3")

    coordinator = MagicMock()
    coordinator.data = {"12345": mock_meter}
    entity = _make_entity(coordinator, mock_meter)

    coordinator.data = {
        "12345": replace(mock_meter, value=312.5, reading_date=date(2025, 6, 1))
    }
    entity._apply_latest_reading()

    for day, value in ((20, 0.0), (21, 0.3), (22, 0.7)):
        coordinator.data = {
            "12345": replace(mock_meter, value=value, reading_date=date(2025, 6, day))
        }
        entity._apply_latest_reading()
        assert entity.native_value == 312.5


async def test_sensor_decrease_without_reading_date_is_ignored(mock_meter):
    """A reading can arrive without a parseable readingDate. It cannot be
    placed in the calendar year, so an annual reset cannot be recognised and
    the value must be dropped — not crash the coordinator callback."""
    mock_meter = replace(mock_meter, meter_type="Radiator", meter_type_code=1, unit="units")

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
    # has_entity_name + the device name determine the generated entity_id: the
    # device is "Water (12345)" and the entity name is "Consumption". Confirmed
    # against a real run — if the device naming in __init__ changes, this
    # string changes with it.
    entity_id = "sensor.water_12345_consumption"
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


# --- the replacement baseline across a restart ----------------------------


def _replaced_pair(mock_meter):
    """The same meter_id before and after Brunata swaps the physical device."""
    old = replace(
        mock_meter,
        meter_type="Water",
        meter_type_code=2,
        unit="m3",
        meter_no="M111",
        mounting_date=datetime(2018, 10, 23, 14, 10, tzinfo=UTC),
        value=312.5,
        reading_date=date(2026, 10, 30),
    )
    new = replace(
        old,
        meter_no="M999",
        mounting_date=datetime(2026, 11, 4, 9, 0, tzinfo=UTC),
        value=0.4,
        reading_date=date(2026, 11, 10),
    )
    return old, new


async def test_replacement_while_restarted_is_still_a_replacement(mock_meter):
    """The bug this restore closes.

    __init__ reads meter_no and mounting_date from whatever the coordinator
    holds at the moment the entity is created. After a swap that happened while
    Home Assistant was down, that is already the *new* meter — so the restored
    high value, the incoming near-zero value and two unchanged-looking metadata
    fields add up to something indistinguishable from a glitch. The reset was
    rejected, and rejected again every hour, until the new meter passed the old
    one's final reading. For a water meter that is years.
    """
    old, new = _replaced_pair(mock_meter)

    coordinator = MagicMock()
    coordinator.data = {"12345": new}
    entity = _make_entity(coordinator, new)

    await _restore(
        entity,
        MagicMock(state="312.5", attributes={"reading_date": "2026-10-30"}),
        BrunataRestoredData(
            meter_no=old.meter_no, mounting_date=old.mounting_date.isoformat()
        ),
    )

    assert entity.native_value == 0.4
    assert entity._meter_no == "M999"
    assert entity._mounting_date == new.mounting_date


async def test_restored_baseline_still_rejects_an_unexplained_decrease(mock_meter):
    """The restore must not turn the guard off. With the same meter number and
    mounting date on both sides, a fall is still a glitch."""
    old, _ = _replaced_pair(mock_meter)

    coordinator = MagicMock()
    coordinator.data = {"12345": replace(old, value=0.4, reading_date=date(2026, 11, 10))}
    entity = _make_entity(coordinator, old)

    await _restore(
        entity,
        MagicMock(state="312.5", attributes={"reading_date": "2026-10-30"}),
        BrunataRestoredData(
            meter_no=old.meter_no, mounting_date=old.mounting_date.isoformat()
        ),
    )

    assert entity.native_value == 312.5


async def test_extra_restore_state_data_round_trips(mock_meter):
    """What is written must be what from_dict() can read back, or the restore
    silently degrades to the behaviour it was added to fix."""
    _, new = _replaced_pair(mock_meter)

    coordinator = MagicMock()
    coordinator.data = {"12345": new}
    entity = _make_entity(coordinator, new)

    stored = entity.extra_restore_state_data.as_dict()
    restored = BrunataRestoredData.from_dict(stored)

    assert restored.meter_no == "M999"
    assert restored.mounting_date == new.mounting_date.isoformat()


@pytest.mark.parametrize("stored", [{}, {"meter_no": "M1"}, "not a dict", None])
async def test_unusable_extra_data_leaves_the_init_baseline_alone(mock_meter, stored):
    """A store written before this class existed has no such keys. That entity
    is in exactly the state it was in before, not in a broken one."""
    _, new = _replaced_pair(mock_meter)

    coordinator = MagicMock()
    coordinator.data = {"12345": new}
    entity = _make_entity(coordinator, new)

    assert BrunataRestoredData.from_dict(stored) is None

    await _restore(entity, None, None)
    assert entity._meter_no == "M999"
    assert entity._mounting_date == new.mounting_date


# --- what counts, and what merely measures --------------------------------
#
# _is_cumulative_unit() takes two strings and returns a bool. Testing it here,
# rather than only through an entity, is what lets the odd corners of Brunata's
# unit table be covered without building a meter that carries them: a unit
# string on its own claims nothing about which meter it arrived on.


@pytest.mark.parametrize(
    "canonical",
    [
        UnitOfVolume.CUBIC_METERS,
        UnitOfVolume.LITERS,
        UnitOfEnergy.KILO_WATT_HOUR,
        UnitOfEnergy.GIGA_JOULE,
    ],
)
def test_a_mapped_unit_counts_up_whatever_brunata_spelled_it(canonical):
    """Once UNIT_MAP has resolved the unit, the raw spelling stops mattering:
    the same litre arrives as "liter" or "l" and must count either way."""
    assert _is_cumulative_unit(canonical, "whatever Brunata sent") is True


@pytest.mark.parametrize(
    ("raw_unit", "expected"),
    [
        # Every allocator spelling carries the same marker. Brunata's table has
        # twenty of them; three are enough to show the marker is what matches
        # rather than the vendor name.
        ("units", True),
        ("Doprimo units", True),
        ("VVME87 units", True),
        # Real entries in Brunata's table that Home Assistant has no constant
        # for and that carry no marker.
        ("Dal", False),
        ("Btu", False),
        ("m³ per hour", False),
        # Entries from the parts of the table this integration never sees,
        # because api/parse.py filters on meter type before a unit is read at all.
        # They are here to pin this function's behaviour on any string, not to
        # suggest a supported meter could arrive carrying one.
        ("°C", False),
        ("%", False),
        ("ppm", False),
        # Nothing at all. api/parse.py drops a meter whose unit did not resolve, so
        # this should not reach the function — the safe answer is still False.
        ("", False),
    ],
)
def test_an_unmapped_unit_counts_up_only_if_it_carries_the_marker(
    raw_unit, expected
):
    """With no Home Assistant constant to go on, the raw spelling is all there
    is. Anything without the marker gets the deliberately safe answer."""
    assert _is_cumulative_unit(None, raw_unit) is expected


@pytest.mark.parametrize(
    ("raw_unit", "expected"),
    [
        # Recognised consumption units, spelled as Brunata's table spells them.
        ("m3", SensorStateClass.TOTAL_INCREASING),
        ("m³", SensorStateClass.TOTAL_INCREASING),
        ("liter", SensorStateClass.TOTAL_INCREASING),
        ("kWh", SensorStateClass.TOTAL_INCREASING),
        ("GJ", SensorStateClass.TOTAL_INCREASING),
        # Allocator units, including the vendor-specific spellings.
        ("units", SensorStateClass.TOTAL_INCREASING),
        ("Doprimo units", SensorStateClass.TOTAL_INCREASING),
        ("Zenner units", SensorStateClass.TOTAL_INCREASING),
        # Real table entries a supported meter could plausibly carry that Home
        # Assistant has no constant for: a volume, an energy and a flow rate.
        ("Dal", SensorStateClass.MEASUREMENT),
        ("Btu", SensorStateClass.MEASUREMENT),
        ("m³ per hour", SensorStateClass.MEASUREMENT),
    ],
)
async def test_state_class_follows_whether_the_unit_accumulates(
    mock_meter, raw_unit, expected
):
    """That the entity wires itself to _is_cumulative_unit(), on units a water
    meter could actually arrive with.

    Every unit here is a real entry in Brunata's table. Temperature and the
    rest of the instrument units are covered against the function above
    instead: putting them on this meter would depict a payload Brunata cannot
    send, and a test fixture gets read as a reading months later.
    """
    entity = _make_entity(MagicMock(), replace(mock_meter, unit=raw_unit))

    assert entity.state_class == expected


@pytest.mark.parametrize(
    "raw_unit", ["m3", "kWh", "units", "Dal", "Btu", "m³ per hour"]
)
async def test_every_meter_keeps_a_state_class(mock_meter, raw_unit):
    """Both classes are recorded in Long Term Statistics — MEASUREMENT stores
    min/max/mean where TOTAL_INCREASING stores a sum. Dropping the state class
    instead would be the one outcome that costs a meter its history."""
    entity = _make_entity(MagicMock(), replace(mock_meter, unit=raw_unit))

    assert entity.state_class is not None


async def test_an_unrecognised_measuring_unit_is_allowed_to_fall(mock_meter):
    """The fallback behaviour for a unit outside water, energy and allocators.

    Dal is decalitres — a volume, so a water meter could carry it, and the one
    entry in Brunata's table that would actually reach this branch. Home
    Assistant has no constant for it, so the guard is off and the value follows
    whatever Brunata reports, including downwards. That is the cost of the
    conservative default, and it is why the reading is not summed either.
    """
    meter = replace(mock_meter, unit="Dal")

    coordinator = MagicMock()
    coordinator.data = {"12345": meter}
    entity = _make_entity(coordinator, meter)

    for value, day in ((1510.4, 1), (1512.0, 2), (1499.8, 3), (1503.1, 4)):
        coordinator.data = {
            "12345": replace(meter, value=value, reading_date=date(2026, 6, day))
        }
        entity._apply_latest_reading()
        assert entity.native_value == value


async def test_a_counting_meter_is_still_guarded(mock_meter):
    """The other half of the same rule: relaxing the guard for measurements
    must not relax it for consumption."""
    meter = replace(mock_meter, meter_type="Water", meter_type_code=2, unit="m3")

    coordinator = MagicMock()
    coordinator.data = {"12345": meter}
    entity = _make_entity(coordinator, meter)

    coordinator.data = {
        "12345": replace(meter, value=312.5, reading_date=date(2026, 6, 1))
    }
    entity._apply_latest_reading()

    coordinator.data = {
        "12345": replace(meter, value=1.0, reading_date=date(2026, 6, 2))
    }
    entity._apply_latest_reading()

    assert entity.native_value == 312.5


# --- log noise ------------------------------------------------------------


async def test_rejected_decrease_is_logged_once_per_run(mock_meter, caplog):
    """The cached value is never lowered, so a decrease rejected once is
    rejected again every poll — 24 identical lines a day, potentially forever.
    The first one carries everything the rest do."""
    meter = replace(mock_meter, meter_type="Water", meter_type_code=2, unit="m3")

    coordinator = MagicMock()
    coordinator.data = {"12345": meter}
    entity = _make_entity(coordinator, meter)

    coordinator.data = {
        "12345": replace(meter, value=312.5, reading_date=date(2026, 6, 1))
    }
    entity._apply_latest_reading()

    with caplog.at_level(logging.WARNING):
        caplog.clear()
        for day in (2, 3, 4):
            coordinator.data = {
                "12345": replace(meter, value=1.0, reading_date=date(2026, 6, day))
            }
            entity._apply_latest_reading()

        assert caplog.text.count("reported a decrease") == 1

        # An accepted reading makes the next rejection news again.
        coordinator.data = {
            "12345": replace(meter, value=313.0, reading_date=date(2026, 6, 5))
        }
        entity._apply_latest_reading()
        coordinator.data = {
            "12345": replace(meter, value=1.0, reading_date=date(2026, 6, 6))
        }
        entity._apply_latest_reading()

    assert caplog.text.count("reported a decrease") == 2


# --- reset dates other than 1 January -------------------------------------
#
# Issue #88: an account whose heat cost allocators are zeroed on 1 June. The
# calendar-year rule cannot see that reset, so the sensor asks Brunata for its
# own daily consumption over the fall. The readings below are the ones in the
# issue: 761 on 31 May, 0 on 1 June.


@pytest.mark.parametrize(
    ("days", "new_value", "expected"),
    [
        # The maintainer's 1 January 2026 reset, read off the live endpoint:
        # a meter with no consumption either side, and one with some.
        ([0.0, 0.0], 0.0, True),
        ([6.0, 7.0], 5.0, True),
        # More on the meter than was used since the period began.
        ([6.0, 7.0], 20.0, False),
        # A fall Brunata did not treat as a reset shows as a negative day.
        ([3.0, -758.0], 0.0, False),
        # Days Brunata could not compute are left out of the sum.
        ([None, 4.0], 4.0, True),
        ([None, 4.0], 5.0, False),
        # No figures is no evidence.
        ([None, None], 0.0, False),
        ([], 0.0, False),
        (None, 0.0, False),
    ],
)
def test_consumption_shows_reset(days, new_value, expected):
    assert _consumption_shows_reset(days, new_value) is expected


def _june_allocator(mock_meter, hass, days):
    """An allocator at 761 on 31 May, attached to hass, with Brunata answering
    `days` (or raising it) when asked about its consumption."""
    meter = replace(
        mock_meter,
        meter_type="Radiator",
        meter_type_code=1,
        unit="units",
        allocation_unit="O",
    )
    coordinator = MagicMock()
    coordinator.data = {
        "12345": replace(meter, value=761.0, reading_date=date(2026, 5, 31))
    }
    if isinstance(days, Exception):
        coordinator.client.async_get_daily_consumption = AsyncMock(side_effect=days)
    else:
        coordinator.client.async_get_daily_consumption = AsyncMock(return_value=days)

    entity = _make_entity(coordinator, meter)
    entity.hass = hass
    entity.async_write_ha_state = MagicMock()
    entity._apply_latest_reading()
    assert entity.native_value == 761.0
    return entity, coordinator, meter


async def test_a_reset_on_another_date_is_accepted_when_brunata_confirms_it(
    hass: HomeAssistant, mock_meter
):
    entity, coordinator, meter = _june_allocator(mock_meter, hass, [0.0, 0.0])

    coordinator.data = {
        "12345": replace(meter, value=0.0, reading_date=date(2026, 6, 1))
    }
    entity._apply_latest_reading()
    # Held back while Brunata is asked.
    assert entity.native_value == 761.0

    await hass.async_block_till_done(wait_background_tasks=True)

    assert entity.native_value == 0.0
    assert entity.extra_state_attributes["reading_date"] == "2026-06-01"
    entity.async_write_ha_state.assert_called_once()
    # Whole local days, from the day of the last accepted reading to the end
    # of the day of the fall.
    coordinator.client.async_get_daily_consumption.assert_awaited_once_with(
        "12345",
        "O",
        dt_util.start_of_local_day(date(2026, 5, 31)),
        dt_util.start_of_local_day(date(2026, 6, 2)),
    )


async def test_a_fall_brunata_does_not_confirm_is_rejected_and_asked_once(
    hass: HomeAssistant, mock_meter, caplog
):
    entity, coordinator, meter = _june_allocator(
        mock_meter, hass, [3.0, -758.0]
    )

    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            coordinator.data = {
                "12345": replace(meter, value=6.0, reading_date=date(2026, 6, 1))
            }
            entity._apply_latest_reading()
            await hass.async_block_till_done(wait_background_tasks=True)

    assert entity.native_value == 761.0
    assert caplog.text.count("reported a decrease") == 1
    # The same fall from the same baseline is not asked about every poll.
    assert coordinator.client.async_get_daily_consumption.await_count == 1


async def test_a_failed_question_is_asked_again_at_the_next_poll(
    hass: HomeAssistant, mock_meter, caplog
):
    entity, coordinator, meter = _june_allocator(
        mock_meter, hass, BrunataConnectionError("down")
    )

    with caplog.at_level(logging.WARNING):
        for _ in range(2):
            coordinator.data = {
                "12345": replace(meter, value=0.0, reading_date=date(2026, 6, 1))
            }
            entity._apply_latest_reading()
            await hass.async_block_till_done(wait_background_tasks=True)

    assert entity.native_value == 761.0
    # Not an answer, so not a warning either.
    assert "reported a decrease" not in caplog.text
    assert coordinator.client.async_get_daily_consumption.await_count == 2


async def test_a_confirmed_reset_also_covers_later_readings(
    hass: HomeAssistant, mock_meter
):
    """A newer reading may be in the coordinator by the time the answer
    arrives. It is past the same reset, so the same answer covers it."""
    entity, coordinator, meter = _june_allocator(mock_meter, hass, [0.0, 0.0])

    coordinator.data = {
        "12345": replace(meter, value=0.0, reading_date=date(2026, 6, 1))
    }
    entity._apply_latest_reading()
    coordinator.data = {
        "12345": replace(meter, value=1.0, reading_date=date(2026, 6, 2))
    }

    await hass.async_block_till_done(wait_background_tasks=True)

    assert entity.native_value == 1.0


async def test_an_allocator_without_an_allocation_unit_is_not_asked_about(
    hass: HomeAssistant, mock_meter, caplog
):
    """Without allocationUnit the request cannot be made, so the fall is
    rejected with the warning straight away, as before."""
    entity, coordinator, meter = _june_allocator(mock_meter, hass, [0.0, 0.0])
    meter = replace(meter, allocation_unit=None)

    with caplog.at_level(logging.WARNING):
        coordinator.data = {
            "12345": replace(meter, value=0.0, reading_date=date(2026, 6, 1))
        }
        entity._apply_latest_reading()
        await hass.async_block_till_done(wait_background_tasks=True)

    assert entity.native_value == 761.0
    assert caplog.text.count("reported a decrease") == 1
    coordinator.client.async_get_daily_consumption.assert_not_awaited()


async def test_a_water_meter_that_falls_is_not_asked_about(
    hass: HomeAssistant, mock_meter
):
    """Only heat cost allocators are reset once a year. A water meter that
    falls without being replaced is a glitch, as before."""
    meter = replace(mock_meter, allocation_unit="K")
    coordinator = MagicMock()
    coordinator.data = {
        "12345": replace(meter, value=312.5, reading_date=date(2026, 5, 31))
    }
    coordinator.client.async_get_daily_consumption = AsyncMock(return_value=[0.0])
    entity = _make_entity(coordinator, meter)
    entity.hass = hass
    entity._apply_latest_reading()

    coordinator.data = {
        "12345": replace(meter, value=0.0, reading_date=date(2026, 6, 1))
    }
    entity._apply_latest_reading()
    await hass.async_block_till_done(wait_background_tasks=True)

    assert entity.native_value == 312.5
    coordinator.client.async_get_daily_consumption.assert_not_awaited()


# --- placement -----------------------------------------------------------
#
# placement is the customer-assigned location label from Brunata's own UI
# (e.g. "Bathroom (Cold)"), fetched separately from the reading itself and
# absent whenever the meter has none set.

async def test_sensor_device_name_uses_placement_when_present(mock_meter):
    """When a placement is available, the device name leads with it, so the
    device is recognisable without opening it."""
    meter = replace(mock_meter, placement="Bathroom (Cold)")
    coordinator = MagicMock()
    coordinator.data = {"12345": meter}
    entity = _make_entity(coordinator, meter)

    assert entity._attr_device_info["name"] == "Water - Bathroom (Cold)"


async def test_sensor_device_name_falls_back_without_placement(mock_meter):
    """No placement set in Brunata keeps the original
    type+ID name rather than showing something blank."""
    coordinator = MagicMock()
    coordinator.data = {"12345": mock_meter}
    entity = _make_entity(coordinator, mock_meter)

    assert entity._attr_device_info["name"] == "Water (12345)"


async def test_sensor_device_carries_the_serial_and_a_link_to_the_portal(mock_meter):
    """The device page shows the meter number and links to Brunata Online.

    The serial is redacted in diagnostics.py, and that is not a contradiction:
    the redaction is about publishing the number in a public issue, not about
    showing an owner their own meter. The link points at the account root
    rather than the readings page, because REFERER_URL is a request header and
    reusing it here would overload what that constant means.
    """
    coordinator = MagicMock()
    coordinator.data = {"12345": mock_meter}
    entity = _make_entity(coordinator, mock_meter)

    assert entity._attr_device_info["serial_number"] == mock_meter.meter_no
    assert entity._attr_device_info["configuration_url"] == BASE_URL


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
    accept/reject decision. A meter whose reading is being rejected still
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


async def test_device_name_follows_a_relabelled_meter(
    hass: HomeAssistant, mock_brunata_client, mock_meter, device_for_meter
):
    """DeviceInfo is only read when the entity is added, so a meter renamed in
    Brunata's own UI used to keep its old device name until the config entry
    was reloaded — while the placement attribute updated on the next poll. The
    attribute saying "Kitchen" next to a device called "Water - Living room" is
    what a user notices."""
    meter = replace(mock_meter, placement="Living room")
    mock_brunata_client.async_get_meters = AsyncMock(return_value={"12345": meter})

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "test@example.com", "password": "password123"},
    )
    entry.add_to_hass(hass)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert device_for_meter(hass, entry, "12345").name == "Water - Living room"

    mock_brunata_client.async_get_meters = AsyncMock(
        return_value={"12345": replace(meter, placement="Kitchen", value=200.0)}
    )
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    device = device_for_meter(hass, entry, "12345")
    assert device.name == "Water - Kitchen"
    # A relabelling must not disturb the meter type shown on the device page.
    assert device.model == "Water"


async def test_device_model_follows_a_meter_type_that_resolves_later(
    hass: HomeAssistant, mock_brunata_client, mock_meter, device_for_meter
):
    """A meter type that could not be resolved has to stop showing a raw code.

    Brunata sends the meter type as a number looked up in a table it serves.
    api/parse.py falls back to the raw number when that lookup fails, deliberately:
    the type reaches the device name and the `model` field on the device page
    and nothing else, since every decision is made on meter_type_code.

    That fallback was only half true. The name was rewritten whenever it
    changed, but the model was written once when the entity was created and
    never again, so a device that came up as "2" kept "2" as its model for the
    life of the config entry — including after the table started answering.
    """
    meter = replace(mock_meter, meter_type="2", placement="Living room")
    mock_brunata_client.async_get_meters = AsyncMock(return_value={"12345": meter})

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "test@example.com", "password": "password123"},
    )
    entry.add_to_hass(hass)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    device = device_for_meter(hass, entry, "12345")
    assert device.name == "2 - Living room"
    assert device.model == "2"

    mock_brunata_client.async_get_meters = AsyncMock(
        return_value={"12345": replace(meter, meter_type="Water", value=200.0)}
    )
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    device = device_for_meter(hass, entry, "12345")
    assert device.name == "Water - Living room"
    assert device.model == "Water"


async def test_sensor_placement_is_cleared_when_it_disappears(mock_meter):
    """A label removed in Brunata's UI comes back as null in the payload. The
    attribute should follow rather than serve one the API no longer reports."""
    meter = replace(mock_meter, placement="Living room")
    coordinator = MagicMock()
    coordinator.data = {"12345": meter}
    entity = _make_entity(coordinator, meter)

    coordinator.data = {"12345": replace(meter, value=200.0, placement=None)}
    entity._apply_latest_reading()

    assert "placement" not in entity.extra_state_attributes


# --- availability ----------------------------------------------------------


async def test_a_meter_that_disappears_from_the_payload_goes_unavailable(mock_meter):
    """Brunata drops a dismounted meter, and _parse_meters() drops it again on
    dismountedDate, so it simply stops appearing in the coordinator's data.

    Reporting it as unavailable is the honest answer. The alternative is a
    device sitting in Home Assistant showing a final reading forever,
    indistinguishable from a working meter.
    """
    coordinator = MagicMock()
    coordinator.data = {"12345": mock_meter}
    coordinator.last_update_success = True

    entity = _make_entity(coordinator, mock_meter)
    entity._apply_latest_reading()
    assert entity.available is True

    coordinator.data = {}
    assert entity.available is False


async def test_a_meter_skipped_for_an_unusable_unit_stays_available(mock_meter):
    """Missing from the payload is not the same as gone.

    api/parse.py drops a meter whose unit code did not resolve, so it cannot become
    an entity carrying "8" where the user had "m³" — Home Assistant would treat
    that as a different measurement and discard the statistics behind the old
    one, permanently. But the meter is still on the wall.

    Going unavailable for it would punch exactly the hole in Long Term
    Statistics that test_a_failed_update_does_not_make_the_sensor_unavailable
    refuses to punch, for the same kind of transient cause. The value stands,
    the sensor stays available, and it resumes on the next poll that resolves.

    If this test ever has to be loosened, that is the moment to think hard.
    """
    coordinator = MagicMock()
    coordinator.data = {"12345": mock_meter}
    coordinator.last_update_success = True
    coordinator.last_parse = ParseReport(frozenset(), 1)

    entity = _make_entity(coordinator, mock_meter)
    entity._apply_latest_reading()
    assert entity.available is True

    # The unit stopped resolving: the meter is gone from the payload, but
    # api/parse.py says it is only skipped.
    coordinator.data = {}
    coordinator.last_parse = ParseReport(frozenset({"12345"}), 1)

    assert entity.available is True
    assert entity.native_value == mock_meter.value


async def test_a_dismounted_meter_still_goes_unavailable(mock_meter):
    """The other half. Relaxing the rule for a skipped meter must not relax it
    for one Brunata has actually taken off the wall — that device showing a
    final reading forever is indistinguishable from a working one."""
    coordinator = MagicMock()
    coordinator.data = {"12345": mock_meter}
    coordinator.last_update_success = True
    coordinator.last_parse = ParseReport(frozenset(), 1)

    entity = _make_entity(coordinator, mock_meter)
    entity._apply_latest_reading()
    assert entity.available is True

    # Gone from the payload, and not among the skipped ids.
    coordinator.data = {}
    coordinator.last_parse = ParseReport(frozenset(), 1)

    assert entity.available is False


async def test_a_failed_update_does_not_make_the_sensor_unavailable(mock_meter):
    """This override exists to *ignore* coordinator.last_update_success, and
    that is the decision the test is here to hold.

    CoordinatorEntity.available would go False on a failed update. For a meter
    polled once an hour over a cloud API, a transient failure would then punch
    a hole in the Long Term Statistics — and statistics cannot be backfilled
    afterwards, so the hole is permanent. The meter still exists and the value
    is still the last one Brunata reported; reading_date is what says how
    fresh it is.

    If this test ever has to be loosened, that is the moment to think hard.
    """
    coordinator = MagicMock()
    coordinator.data = {"12345": mock_meter}
    coordinator.last_update_success = True

    entity = _make_entity(coordinator, mock_meter)
    entity._apply_latest_reading()
    assert entity.available is True

    # The coordinator keeps the previous data on failure, which is exactly the
    # state being modelled here.
    coordinator.last_update_success = False
    assert entity.available is True


async def test_a_meter_that_has_never_reported_is_unavailable(mock_meter):
    """The entity is created for a meter with no reading — so it picks one up
    when Brunata eventually sends it — but it must not present as a working
    sensor with an empty state in the meantime."""
    meter = replace(mock_meter, value=None, reading_date=None)
    coordinator = MagicMock()
    coordinator.data = {"12345": meter}
    coordinator.last_update_success = True

    entity = _make_entity(coordinator, meter)
    entity._apply_latest_reading()

    assert entity.native_value is None
    assert entity.available is False


async def test_availability_survives_a_coordinator_with_no_data_yet(mock_meter):
    """coordinator.data is None before the first refresh completes. A restored
    value must not be reported as unavailable just because the first poll has
    not landed."""
    coordinator = MagicMock()
    coordinator.data = None
    coordinator.last_update_success = True

    entity = _make_entity(coordinator, mock_meter)
    entity._attr_native_value = 151.037

    assert entity.available is True


async def test_transmitting_is_cleared_when_the_meter_disappears(mock_meter):
    """`transmitting` is a claim about the meter right now.

    A dismounted meter is dropped from the payload, and the attribute used to
    keep whatever it last said — "true" on a meter that was physically taken
    off the wall. The reading is deliberately kept, because it is the last
    thing the meter really registered and dropping it would put a hole in the
    statistics; the claim about the present is not.
    """
    coordinator = MagicMock()
    coordinator.data = {"12345": mock_meter}
    coordinator.last_update_success = True

    entity = _make_entity(coordinator, mock_meter)
    entity._apply_latest_reading()
    assert entity.extra_state_attributes["transmitting"] is True

    coordinator.data = {}
    entity._apply_latest_reading()

    # Dropped from the attributes entirely rather than reported as None:
    # extra_state_attributes only includes what it actually knows, so an
    # unknown value has no key. Asserting `is None` on the key would raise
    # KeyError, not fail — which is why the absence is asserted directly.
    assert "transmitting" not in entity.extra_state_attributes
    assert entity.native_value == mock_meter.value
    assert entity.available is False

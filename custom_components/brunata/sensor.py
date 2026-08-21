"""Support for Brunata meters."""
from __future__ import annotations

import logging
from datetime import date, datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import BrunataConfigEntry, BrunataDataUpdateCoordinator
from .api import BrunataMeter
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Meter types whose value may legitimately drop. Brunata reports heat cost
# allocators with meter_type "Radiator", and zeroes them on 1 January.
# Water and energy meters are never reset, so a decrease on those is always an
# API glitch. Deliberately a deny-list: anything not matching here — including
# meter types we don't recognise — keeps the strict "never counts down" guard.
# Only extend this with types confirmed to reset.
RESETTING_METER_TYPES = ("radiator",)

# Brunata reports units as free-form strings whose casing is not guaranteed
# ("kWh", "KWH", "l", "L", "m3", "m³"). Map them onto Home Assistant's
# canonical unit constants: a device class combined with a non-canonical unit
# string is rejected by HA's unit validation, which logs an error and discards
# the entity's long term statistics. Keys are lowercased and stripped.
UNIT_MAP: dict[str, str] = {
    "m3": UnitOfVolume.CUBIC_METERS,
    "m³": UnitOfVolume.CUBIC_METERS,
    "l": UnitOfVolume.LITERS,
    "kwh": UnitOfEnergy.KILO_WATT_HOUR,
    "mwh": UnitOfEnergy.MEGA_WATT_HOUR,
}

VOLUME_UNITS = (UnitOfVolume.CUBIC_METERS, UnitOfVolume.LITERS)
ENERGY_UNITS = (UnitOfEnergy.KILO_WATT_HOUR, UnitOfEnergy.MEGA_WATT_HOUR)

# Fallback for the rare case where Brunata omits meterUnit entirely. Heat cost
# allocators — the only meters that could plausibly arrive without a unit —
# report "units", so that is the sensible guess and keeps such a sensor
# consistent with its siblings. Note this is only a fallback for a *missing*
# field: a meter that reports "units" normally takes the pass-through branch
# below, which deliberately preserves Brunata's own capitalisation.
FALLBACK_UNIT = "units"


def _as_iso(value) -> str | None:
    """Return a reading date as an ISO string.

    The API hands us date objects, while a state restored after a restart
    yields the string it was serialised as. Normalising here keeps the
    reading_date attribute a single type across restarts, so templates and
    automations reading it don't break on reload.
    """
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _as_date(value) -> date | None:
    """Return a reading date as a date object, or None if it can't be parsed.

    Used only for the calendar-year comparison in _is_plausible_reset(). A
    state restored after a restart hands us the ISO string it was serialised
    as, so it has to be parsed back; anything unparseable simply disables the
    year rule and falls back to the December/January window.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BrunataConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Brunata sensors based on a config entry."""
    _LOGGER.debug("Setting up Brunata sensors for entry %s", entry.entry_id)
    coordinator = entry.runtime_data

    known_meter_ids: set[str] = set()

    @callback
    def _add_new_meters() -> None:
        """Add sensor entities for any newly discovered meters."""
        new_entities = []
        for meter_id, meter in coordinator.data.items():
            if meter_id not in known_meter_ids:
                _LOGGER.debug("Creating BrunataSensor for meter %s", meter_id)
                known_meter_ids.add(meter_id)
                new_entities.append(BrunataSensor(coordinator, meter))
        if new_entities:
            _LOGGER.debug("Adding %s new entities", len(new_entities))
            async_add_entities(new_entities)

    _add_new_meters()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_meters))

class BrunataSensor(
    CoordinatorEntity[BrunataDataUpdateCoordinator], RestoreEntity, SensorEntity
):
    """Representation of a Brunata meter."""

    def __init__(
        self, coordinator: BrunataDataUpdateCoordinator, meter: BrunataMeter
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._meter_id = meter.meter_id
        self._attr_unique_id = f"brunata_{self._meter_id}_consumption"
        # Cache the last known good reading so the sensor keeps its value (and
        # stays available) between the infrequent API updates instead of going
        # unavailable, which would break statistics rows. Updated only from
        # _apply_latest_reading(), never from a property.
        self._attr_native_value = None
        # Kept as an ISO string for the state attribute, and as a date object
        # for the year comparison in _is_plausible_reset().
        self._last_reading_date: str | None = None
        self._last_reading_day: date | None = None
        self._attr_has_entity_name = True
        self._attr_translation_key = "consumption"

        # Resolve the reported unit to a canonical Home Assistant unit.
        raw_unit = meter.unit.strip()
        meter_type = meter.meter_type.lower()
        unit = UNIT_MAP.get(raw_unit.lower())

        if unit is not None:
            self._attr_native_unit_of_measurement = unit
        elif not raw_unit:
            # meterUnit missing from the API response — see FALLBACK_UNIT.
            self._attr_native_unit_of_measurement = FALLBACK_UNIT
        else:
            # Unrecognised unit: pass it through unchanged, but deliberately
            # claim no device class for it — HA would reject the combination.
            self._attr_native_unit_of_measurement = raw_unit

        # Determine device class and icon from the canonical unit.
        if unit in VOLUME_UNITS:
            # SensorDeviceClass.GAS only accepts volume units like m³, never
            # litres, so a litre-reporting gas meter stays on WATER.
            if "gas" in meter_type and unit == UnitOfVolume.CUBIC_METERS:
                self._attr_device_class = SensorDeviceClass.GAS
            else:
                self._attr_device_class = SensorDeviceClass.WATER
            self._attr_icon = "mdi:water"
        elif unit in ENERGY_UNITS:
            self._attr_device_class = SensorDeviceClass.ENERGY
            self._attr_icon = "mdi:lightning-bolt"
        else:
            self._attr_icon = "mdi:gauge"

        # Water/energy meter readings only ever increase, while radiator meters
        # reset once a year. TOTAL_INCREASING covers both: it lets HA compute
        # hourly sums and aggregate consumption, and its statistics engine
        # already knows how to handle a periodic reset to zero.
        self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        self._attr_suggested_display_precision = 2

        # Whether a decreasing reading is plausible for this meter at all.
        # See RESETTING_METER_TYPES and _accept_reading().
        self._may_reset = any(k in meter_type for k in RESETTING_METER_TYPES)

        # Group under a device per meter
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"brunata_{self._meter_id}")},
            name=f"Brunata {meter.meter_type} ({self._meter_id})",
            manufacturer="Brunata",
            model=meter.meter_type,
        )
        _LOGGER.debug(
            "Initialized BrunataSensor for meter %s (%s)",
            self._meter_id,
            meter.meter_type,
        )

    async def async_added_to_hass(self) -> None:
        """Restore the last known reading across restarts and reloads.

        The cached value and reading date are plain in-memory attributes, so
        they reset to None whenever this entity is torn down and recreated — a
        HA restart or any reload. available() would then depend entirely on the
        coordinator already holding a fresh reading for this meter at the exact
        moment the new entity is created, which is more likely to be
        momentarily empty for meter types that report less often (heat cost
        allocators vs. water). Restoring from HA's own last known state closes
        that gap, and gives the decrease guard the baseline it compares against.
        """
        await super().async_added_to_hass()

        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state not in (
            None,
            "unknown",
            "unavailable",
        ):
            try:
                restored = float(last_state.state)
            except (TypeError, ValueError):
                _LOGGER.debug(
                    "Meter %s: ignoring non-numeric restored state %r",
                    self._meter_id,
                    last_state.state,
                )
            else:
                self._attr_native_value = restored
                restored_date = last_state.attributes.get("reading_date")
                self._last_reading_date = _as_iso(restored_date)
                self._last_reading_day = _as_date(restored_date)

        # Apply whatever the coordinator already holds, now that the restored
        # value is in place as the baseline.
        self._apply_latest_reading()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator.

        C3: the accept/reject decision lives here rather than in the
        native_value property. A property is read by Home Assistant on every
        state write and in an order that is not guaranteed relative to
        available() and extra_state_attributes, so mutating state from inside
        one made the reset logic run an unpredictable number of times per
        update. Doing it once, here, keeps the properties pure reads.
        """
        self._apply_latest_reading()
        super()._handle_coordinator_update()

    @callback
    def _apply_latest_reading(self) -> None:
        """Fold the coordinator's latest reading into the cached state.

        How often a fresh reading is available depends on the meter's
        reporting setup — this can be hourly when there's been consumption, or
        as infrequently as once a day. When there is no fresh reading the
        cached value is simply left alone, so the sensor never goes
        unknown/unavailable and statistics stay intact.
        """
        meter = (self.coordinator.data or {}).get(self._meter_id)
        if meter is None or meter.value is None:
            return

        if not self._accept_reading(meter.value, meter.reading_date):
            return

        self._attr_native_value = meter.value
        self._last_reading_date = _as_iso(meter.reading_date)
        self._last_reading_day = meter.reading_date

    def _accept_reading(self, value, reading_date) -> bool:
        """Decide whether a reading should replace the cached value.

        An increase is always accepted. A decrease is only accepted for meter
        types that Brunata actually resets — heat cost allocators, which are
        zeroed on 1 January. Water and energy meters are never reset, so any
        decrease on those is an API glitch and is discarded; accepting it under
        TOTAL_INCREASING would make Home Assistant record a false consumption
        spike on the way back up.

        The reset itself is recognised by _is_plausible_reset(), which is
        deliberately more forgiving than an exact 31 December / 1 January
        match: see that method.
        """
        previous = self._attr_native_value

        if previous is None or value >= previous:
            return True

        if not self._may_reset:
            _LOGGER.debug(
                "Meter %s reported a decrease (%s -> %s) — ignoring it, "
                "this meter type is never reset",
                self._meter_id,
                previous,
                value,
            )
            return False

        if not self._is_plausible_reset(reading_date):
            _LOGGER.warning(
                "Meter %s reported a decrease (%s -> %s) on %s, outside the "
                "new year reset window — ignoring it as a glitch",
                self._meter_id,
                previous,
                value,
                reading_date,
            )
            return False

        _LOGGER.info(
            "Meter %s reset detected: %s -> %s (reading date %s)",
            self._meter_id,
            previous,
            value,
            reading_date,
        )
        return True

    def _is_plausible_reset(self, reading_date) -> bool:
        """Return True if a decrease on this date is a plausible annual reset.

        Heat cost allocators are zeroed on 1 January, but the first reading
        Brunata publishes afterwards is not necessarily dated 1 January — these
        meters report infrequently, so it can arrive dated several days or
        weeks into the new year. Matching only (12, 31) and (1, 1) meant such a
        reading was rejected as a glitch, and because the cached value is never
        lowered, every reading for the rest of the year was rejected too: the
        sensor froze at the pre-reset value until the new period happened to
        exceed it.

        A decrease is therefore treated as the annual reset when either:

        * the reading date has crossed into a later calendar year than the last
          accepted reading — this is the reliable signal and needs no window at
          all; or
        * the reading falls on 31 December or anywhere in January, which covers
          the case where the previous reading date is unknown (e.g. a state
          restored from before this attribute was recorded).
        """
        last_day = self._last_reading_day
        if last_day is not None and reading_date.year > last_day.year:
            return True

        return reading_date.month == 1 or (reading_date.month, reading_date.day) == (
            12,
            31,
        )

    @property
    def available(self) -> bool:
        """Stay available as long as we have ever seen a valid reading."""
        return self._attr_native_value is not None

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        if self._last_reading_date is not None:
            return {
                "reading_date": self._last_reading_date,
            }
        return {}

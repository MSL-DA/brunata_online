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
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Meter types whose value may legitimately drop. Brunata's API reports heat
# cost allocators with meter_type "Radiator", and
# resets them to zero at the end of each accounting year.
# Deliberately a deny-list: anything not matching here — including meter types
# we don't recognise — keeps the strict "never counts down" guard. Only extend
# this with types confirmed to reset.
RESETTING_METER_TYPES = ("radiator",)

# A decrease is only accepted as a reset when the reading's date falls on one
# of these (month, day) pairs — Brunata's year-end reset happens on Dec 31 or
# Jan 1. A decrease reported on any other date is treated as an API glitch,
# regardless of its size.
RESET_WINDOW_MONTH_DAYS = {(12, 31), (1, 1)}

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

# Unit used for meters that report no unit at all, e.g. heat cost allocators.
UNITLESS_UNIT = "pts"


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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Brunata sensors based on a config entry."""
    _LOGGER.debug("Setting up Brunata sensors for entry %s", entry.entry_id)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    known_meter_ids: set[str] = set()

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

class BrunataSensor(CoordinatorEntity, RestoreEntity, SensorEntity):
    """Representation of a Brunata meter."""

    def __init__(self, coordinator, meter):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._meter_id = meter._meter_id
        self._attr_unique_id = f"brunata_{self._meter_id}_consumption"
        # Cache the last known good reading so the sensor keeps its value (and
        # stays available) between the infrequent API updates instead of going
        # unavailable, which would break statistics rows.
        self._last_value = None
        self._last_reading_date = None
        self._attr_has_entity_name = True
        self._attr_translation_key = "consumption"
        self._attr_suggested_object_id = f"brunata_{self._meter_id}_consumption"

        # Resolve the reported unit to a canonical Home Assistant unit.
        raw_unit = (meter.meter_unit or "").strip()
        meter_type = (meter.meter_type or "").lower()
        unit = UNIT_MAP.get(raw_unit.lower())

        if unit is not None:
            self._attr_native_unit_of_measurement = unit
        elif not raw_unit:
            # Meters without a unit (e.g. heat cost allocators) report points.
            self._attr_native_unit_of_measurement = UNITLESS_UNIT
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

        # Whether a decreasing reading is plausible for this meter. See
        # RESETTING_METER_TYPES and native_value().
        self._may_reset = any(k in meter_type for k in RESETTING_METER_TYPES)

        # Group under a device per meter
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"brunata_{self._meter_id}")},
            name=f"Brunata {meter.meter_type} ({self._meter_id})",
            manufacturer="Brunata",
            model=meter.meter_type,
        )
        _LOGGER.debug("Initialized BrunataSensor for meter %s (%s)", self._meter_id, meter.meter_type)

    async def async_added_to_hass(self) -> None:
        """Restore the last known reading across restarts and reloads.

        _last_value/_last_reading_date are plain in-memory attributes, so
        they reset to None whenever this entity is torn down and recreated
        — a HA restart or any reload. available() then depends entirely on
        the coordinator already having a fresh meter.latest_reading at the
        exact moment the new entity is created, which is more likely to be
        momentarily empty for meter types that report less often (heat cost
        allocators vs. water). Restoring from HA's own last known state
        closes that gap.
        """
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is None or last_state.state in (None, "unknown", "unavailable"):
            return
        try:
            self._last_value = float(last_state.state)
        except (TypeError, ValueError):
            return
        # Already an ISO string here, matching what native_value stores.
        self._last_reading_date = _as_iso(last_state.attributes.get("reading_date"))

    @property
    def native_value(self):
        """Return the state of the sensor.
        
        How often a fresh reading is available depends on the meter's
        reporting setup — this can be hourly when there's been consumption,
        or as infrequently as once a day for some setups. When there is no
        fresh reading we keep returning the last known value instead of
        None, so the sensor never goes unknown/unavailable and statistics
        stay intact.
        """
        meter = self.coordinator.data.get(self._meter_id)
        if meter and meter.latest_reading:
            value = meter.latest_reading.value
            reading_date = meter.latest_reading.date

            if self._last_value is None or value >= self._last_value:
                accept = True
            elif self._may_reset and (reading_date.month, reading_date.day) in RESET_WINDOW_MONTH_DAYS:
                # Heat cost allocator meters (meter_type "Radiator") are reset
                # by Brunata at the end of the accounting year, so a drop
                # reported on Dec 31 or Jan 1 is the real new state.
                _LOGGER.info(
                    "Meter %s reset detected: %s -> %s",
                    self._meter_id,
                    self._last_value,
                    value,
                )
                accept = True
            else:
                # Anything else counting down is an API glitch. Keep the last
                # value so HA doesn't read it as a reset and emit a false spike.
                accept = False
                if self._may_reset:
                    _LOGGER.warning(
                        "Meter %s reported a decrease (%s -> %s) outside the "
                        "Dec 31/Jan 1 reset window — ignoring it as a glitch",
                        self._meter_id,
                        self._last_value,
                        value,
                    )
                else:
                    _LOGGER.debug(
                        "Meter %s reported a decrease (%s -> %s) — ignoring it, "
                        "this meter type never counts down",
                        self._meter_id,
                        self._last_value,
                        value,
                    )

            if accept:
                self._last_value = value
                self._last_reading_date = _as_iso(reading_date)
        return self._last_value

    @property
    def available(self) -> bool:
        """Stay available as long as we have ever seen a valid reading."""
        if self._last_value is not None:
            return True
        meter = self.coordinator.data.get(self._meter_id)
        return bool(meter and meter.latest_reading)

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        if self._last_reading_date is not None:
            return {
                "reading_date": self._last_reading_date,
            }
        return {}

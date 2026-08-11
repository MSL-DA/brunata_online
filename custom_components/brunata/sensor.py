"""Support for Brunata meters."""
from __future__ import annotations

import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Meter types whose value may legitimately drop. Brunata resets radiator/heat
# cost allocator meters to (near) zero at the end of each accounting year.
# Deliberately a deny-list: anything not matching here — including meter types
# we don't recognise — keeps the strict "never counts down" guard. Only extend
# this with types confirmed to reset.
RESETTING_METER_TYPES = ("radiator", "allocator")

# A decrease is only accepted as a reset when the reading's date falls on one
# of these (month, day) pairs — Brunata's year-end reset happens on Dec 31 or
# Jan 1. A decrease reported on any other date is treated as an API glitch,
# regardless of its size.
RESET_WINDOW_MONTH_DAYS = {(12, 31), (1, 1)}

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

class BrunataSensor(CoordinatorEntity, SensorEntity):
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

        # Handle unit and map m3 to m³
        raw_unit = meter.meter_unit or ""
        unit = raw_unit.lower()
        if unit == "m3":
            self._attr_native_unit_of_measurement = "m³"
        elif not unit:
            # For meters without unit (e.g. radiator meters) we use 'pts' (points)
            self._attr_native_unit_of_measurement = "pts"
        else:
            self._attr_native_unit_of_measurement = raw_unit

        # Determine device class and icon
        meter_type = (meter.meter_type or "").lower()
        if unit in ["m³", "m3", "l"]:
            if "gas" in meter_type:
                self._attr_device_class = SensorDeviceClass.GAS
            else:
                self._attr_device_class = SensorDeviceClass.WATER
            self._attr_icon = "mdi:water"
        elif unit in ["kwh", "mwh"]:
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

    @property
    def native_value(self):
        """Return the state of the sensor.

        The API only refreshes once a day (or less). When there is no fresh
        reading we keep returning the last known value instead of None, so the
        sensor never goes unknown/unavailable and statistics stay intact.
        """
        meter = self.coordinator.data.get(self._meter_id)
        if meter and meter.latest_reading:
            value = meter.latest_reading.value
            reading_date = meter.latest_reading.date

            if self._last_value is None or value >= self._last_value:
                accept = True
            elif self._may_reset and (reading_date.month, reading_date.day) in RESET_WINDOW_MONTH_DAYS:
                # Radiator/allocator meters are reset by Brunata at the end of
                # the accounting year, so a drop reported on Dec 31 or Jan 1
                # is the real new state.
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
                self._last_reading_date = reading_date
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

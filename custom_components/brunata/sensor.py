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
            # Fallback for meters that report no unit at all. Confirmed via
            # meter_type == "Radiator" that heat-cost-allocator meters do NOT
            # hit this branch — their API response includes unit == "units",
            # which is handled by the else branch below. This branch is kept
            # as a safety net in case some other, currently unknown meter
            # type reports an empty unit string.
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

        # A meter reading only ever increases and never resets, so TOTAL_INCREASING
        # lets HA correctly compute hourly sums and aggregate consumption over time.
        self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        self._attr_suggested_display_precision = 2

        # Radiator/heat-cost-allocator meters reset annually at year-end per
        # Brunata's own behaviour (confirmed via meter_type == "Radiator"),
        # unlike water/energy meters which never decrease. Track this so
        # native_value() can tell a real reset apart from a transient glitch.
        self._periodically_resets = "radiator" in meter_type

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
            # A water/energy meter never counts down, so a lower value there
            # is treated as a glitch and ignored, keeping the last value so
            # HA doesn't read it as a reset and emit a false spike.
            # Radiator/heat-cost-allocator meters (self._periodically_resets)
            # genuinely reset at year-end, so a lower value for those is
            # accepted as the new, correct state.
            if (
                self._last_value is None
                or value >= self._last_value
                or self._periodically_resets
            ):
                self._last_value = value
                self._last_reading_date = meter.latest_reading.date
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

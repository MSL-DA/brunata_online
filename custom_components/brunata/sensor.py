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

# Meter types that are reset on a schedule. Brunata reports heat cost
# allocators with meter_type "Radiator" and zeroes them on 1 January, so a
# decrease around new year needs no further evidence to be believed.
#
# This is not a list of the only meters that can ever drop: *any* meter starts
# over from zero when the physical device is replaced (worn out, flat battery),
# and that happens at any time of year. Those decreases are recognised by
# _accept_reading() instead.
ANNUAL_RESET_METER_TYPES = ("radiator",)

# How many distinct reading dates must report a value below the cached one
# before it is accepted as a real reset rather than an API glitch. A glitch
# corrects itself on the next reading; a replaced meter keeps counting up from
# zero, so every reading after it stays below the old value.
RESET_CONFIRMATION_READINGS = 3

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

    Used only for the calendar-year comparison in _is_annual_reset(). A
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
        # The physical meter behind this meter_id. Brunata keeps the meter_id
        # when a device is swapped out, so a change here means the hardware was
        # replaced and the new device counts from zero.
        self._meter_no = meter.meter_no
        # Reading dates that have reported a value below the cached one. Not
        # restored across restarts — worst case the confirmation starts over.
        self._pending_reset_dates: set[date] = set()
        # Cache the last known good reading so the sensor keeps its value (and
        # stays available) between the infrequent API updates instead of going
        # unavailable, which would break statistics rows. Updated only from
        # _apply_latest_reading(), never from a property.
        self._attr_native_value = None
        # Kept as an ISO string for the state attribute, and as a date object
        # for the year comparison in _is_annual_reset().
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

        # Readings climb until the meter is zeroed — every 1 January for a heat
        # cost allocator, and whenever the physical device is replaced for any
        # meter. TOTAL_INCREASING is built for exactly that: it lets HA compute
        # hourly sums and aggregate consumption, and its statistics engine
        # already knows how to handle a drop back to zero.
        self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        # Heat cost allocators report "units" as whole numbers (e.g. 38.00) —
        # the decimals are an artifact of the API, not meaningful precision,
        # so the display is rounded to an integer. Water keeps 3 decimals;
        # energy keeps 2. This only affects display: native_value and Long
        # Term Statistics retain the full float either way.
        if unit in VOLUME_UNITS:
            self._attr_suggested_display_precision = 3
        elif unit in ENERGY_UNITS:
            self._attr_suggested_display_precision = 2
        else:
            self._attr_suggested_display_precision = 0

        # Whether this meter is zeroed every 1 January. See
        # ANNUAL_RESET_METER_TYPES and _accept_reading().
        self._resets_annually = any(
            k in meter_type for k in ANNUAL_RESET_METER_TYPES
        )

        # Group under a device per meter. Brunata's own UI lets a customer
        # label each meter with its physical location ("placement", e.g.
        # "Bad/Køkken (Koldt)") — when that label is available, lead with it
        # so the device is recognisable without opening it; otherwise fall
        # back to the generic type+ID name used before placement existed.
        self._placement = meter.placement
        device_name = (
            f"Brunata {meter.meter_type} - {meter.placement}"
            if meter.placement
            else f"Brunata {meter.meter_type} ({self._meter_id})"
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"brunata_{self._meter_id}")},
            name=device_name,
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

        if not self._accept_reading(meter):
            return

        self._attr_native_value = meter.value
        self._meter_no = meter.meter_no
        self._pending_reset_dates.clear()
        self._last_reading_date = _as_iso(meter.reading_date)
        self._last_reading_day = meter.reading_date

    def _accept_reading(self, meter: BrunataMeter) -> bool:
        """Decide whether a reading should replace the cached value.

        An increase is always accepted. A decrease has three ways to be
        believed, in order of how solid the evidence is:

        1. The meter number changed. The physical device was replaced, so the
           new one legitimately starts near zero — whatever its type, whatever
           the date.
        2. It is a heat cost allocator around new year. Those are zeroed on
           1 January, every year, without exception.
        3. The same lower value keeps coming back. A replacement Brunata
           reports under the old meter number, or any other reset we can't
           recognise directly, still leaves a lasting trace: every reading from
           the new device stays below the old value. An API glitch does not —
           the next reading is back where it belongs. See
           _decrease_is_confirmed().

        Anything else is discarded: accepting a glitch under TOTAL_INCREASING
        would make Home Assistant record a false consumption spike on the way
        back up.
        """
        previous = self._attr_native_value
        value = meter.value

        if previous is None or value >= previous:
            self._pending_reset_dates.clear()
            return True

        if meter.meter_no != self._meter_no:
            _LOGGER.info(
                "Meter %s was replaced (meter number %s -> %s): accepting the "
                "reset from %s to %s",
                self._meter_id,
                self._meter_no,
                meter.meter_no,
                previous,
                value,
            )
            return True

        if self._resets_annually and self._is_annual_reset(meter.reading_date):
            _LOGGER.info(
                "Meter %s annual reset detected: %s -> %s (reading date %s)",
                self._meter_id,
                previous,
                value,
                meter.reading_date,
            )
            return True

        return self._decrease_is_confirmed(previous, value, meter.reading_date)

    def _decrease_is_confirmed(self, previous, value, reading_date) -> bool:
        """Return True once a decrease has been seen often enough to be real.

        Meters are replaced when they wear out or the battery runs low, which
        happens at any time of year and on any meter type. If Brunata reports
        the replacement under the same meter number, the only thing separating
        it from a glitch is that it persists: the new device counts up from
        zero, so reading after reading stays below the old value.

        Distinct reading dates are counted rather than updates, because the
        coordinator polls far more often than the meters report — re-serving
        the same reading is not new evidence. A reading without a usable date
        can't contribute, so it is simply ignored.
        """
        if reading_date is None:
            _LOGGER.debug(
                "Meter %s reported a decrease (%s -> %s) without a reading "
                "date — ignoring it",
                self._meter_id,
                previous,
                value,
            )
            return False

        self._pending_reset_dates.add(reading_date)
        seen = len(self._pending_reset_dates)

        if seen < RESET_CONFIRMATION_READINGS:
            _LOGGER.debug(
                "Meter %s reported a decrease (%s -> %s) on %s — holding the "
                "cached value, %s of %s reading dates seen so far",
                self._meter_id,
                previous,
                value,
                reading_date,
                seen,
                RESET_CONFIRMATION_READINGS,
            )
            return False

        _LOGGER.warning(
            "Meter %s has read below %s on %s separate reading dates — "
            "treating this as a reset and adopting %s. If the meter was not "
            "replaced or read out, check the meter in Brunata Online.",
            self._meter_id,
            previous,
            seen,
            value,
        )
        return True

    def _is_annual_reset(self, reading_date) -> bool:
        """Return True if a decrease on this date is the annual 1 January reset.

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

        A reading with no usable date can't be placed in the year at all, so it
        falls through to the confirmation rule instead.
        """
        if reading_date is None:
            return False

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
        attributes = {}
        if self._last_reading_date is not None:
            attributes["reading_date"] = self._last_reading_date
        if self._placement is not None:
            attributes["placement"] = self._placement
        return attributes

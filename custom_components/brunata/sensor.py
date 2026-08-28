"""Support for Brunata meters."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import BrunataConfigEntry, BrunataDataUpdateCoordinator
from .api import BrunataMeter, _parse_reading_date, _parse_timestamp
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Meter types that are reset on a schedule. Brunata zeroes heat cost
# allocators on 1 January, so a decrease around new year needs no further
# evidence to be believed.
#
# Matched on Brunata's numeric meterType, not on the name it resolves to. The
# name comes from the locale lookup table — a translation Brunata owns, fetched
# in whatever language api.py's LOCALE asks for. Matching "radiator" in it was
# a rule that would switch itself off silently the day Brunata relabelled the
# entry to anything else, and because the cached value is never lowered, the
# 1 January decrease would then be rejected as a glitch and *every* reading for
# the rest of the year rejected with it. The code is the same value
# SUPPORTED_METER_TYPES is enforced on, and it is read off the payload rather
# than translated.
#
# This is not a list of the only meters that can ever drop: *any* meter starts
# over from zero when the physical device is replaced (worn out, flat battery),
# and that happens at any time of year. Those decreases are recognised by
# _accept_reading() instead.
#
#   1 = heat cost allocator (radiator). Read off live account data.
ANNUAL_RESET_METER_TYPES = frozenset({1})


# Keys are the entries in Brunata's measurementUnit table, lowercased and
# stripped, because the casing is not guaranteed ("kWh", "KWH", "l", "L",
# "m3", "m³"). Values are Home Assistant's canonical unit constants: a device
# class combined with a non-canonical unit string is rejected by HA's unit
# validation, which logs an error and discards the entity's long term
# statistics.
#
# That table has 96 slots and spans far more than metering. Only entries with
# a Home Assistant equivalent are mapped; everything else passes through
# verbatim and gets no device class, which is the correct outcome rather than
# a limitation — see _is_cumulative_unit() for how such a reading is still
# handled safely.
UNIT_MAP: dict[str, str] = {
    # Volume
    "m3": UnitOfVolume.CUBIC_METERS,
    "m³": UnitOfVolume.CUBIC_METERS,
    "liter": UnitOfVolume.LITERS,
    # Brunata spells litres out; the abbreviation is kept in case that changes.
    "l": UnitOfVolume.LITERS,
    # Energy. GJ and MWh are the usual units for Danish district heating.
    "wh": UnitOfEnergy.WATT_HOUR,
    "kwh": UnitOfEnergy.KILO_WATT_HOUR,
    "mwh": UnitOfEnergy.MEGA_WATT_HOUR,
    "j": UnitOfEnergy.JOULE,
    "kj": UnitOfEnergy.KILO_JOULE,
    "mj": UnitOfEnergy.MEGA_JOULE,
    "gj": UnitOfEnergy.GIGA_JOULE,
    "kcal": UnitOfEnergy.KILO_CALORIE,
    "mcal": UnitOfEnergy.MEGA_CALORIE,
    "gcal": UnitOfEnergy.GIGA_CALORIE,
    # Deliberately absent: "Btu" has no Home Assistant equivalent, and the
    # remaining entries in Brunata's table are outside what this integration
    # is built for. What state class an unmapped reading gets is decided by
    # _is_cumulative_unit() below, not here.
}

VOLUME_UNITS = (UnitOfVolume.CUBIC_METERS, UnitOfVolume.LITERS)
ENERGY_UNITS = (
    UnitOfEnergy.WATT_HOUR,
    UnitOfEnergy.KILO_WATT_HOUR,
    UnitOfEnergy.MEGA_WATT_HOUR,
    UnitOfEnergy.JOULE,
    UnitOfEnergy.KILO_JOULE,
    UnitOfEnergy.MEGA_JOULE,
    UnitOfEnergy.GIGA_JOULE,
    UnitOfEnergy.KILO_CALORIE,
    UnitOfEnergy.MEGA_CALORIE,
    UnitOfEnergy.GIGA_CALORIE,
)

# Fallback for the rare case where Brunata omits the unit field entirely. Heat
# cost allocators — the only meters that could plausibly arrive without a unit
# — report "units", so that is the sensible guess and keeps such a sensor
# consistent with its siblings. Note this is only a fallback for a *missing*
# field: a meter that reports "units" normally takes the pass-through branch
# below, which deliberately preserves Brunata's own capitalisation.
FALLBACK_UNIT = "units"

# Index 0 of Brunata's measurementUnit table is the literal string
# "undefined". A meter pointing at it has no unit stated, so it is treated the
# same as a missing one rather than being labelled "undefined" in the UI.
UNDEFINED_UNIT = "undefined"

# Substrings that identify an allocator unit — a count that only ever climbs.
# Brunata's table carries a dozen vendor-specific variants ("Doprimo units",
# "Zenner units"), and FALLBACK_UNIT is one too, so matching the marker rather
# than listing the spellings keeps a new vendor from being misclassified.
ALLOCATOR_UNIT_MARKERS = ("unit", "pts")


def _is_cumulative_unit(canonical: str | None, raw_unit: str) -> bool:
    """Return True if readings in this unit only ever climb.

    This is the one decision the state class and the decrease guard both hang
    off, so it is written once. Volume and energy are consumption: they count
    up until the meter is zeroed. Allocator units do the same. This
    integration is built for those three — water, energy, and heat cost
    allocators — nothing else is tested or supported.

    An unrecognised unit is treated as *not* cumulative. That is a defensive
    default for whatever Brunata's API might one day report outside the
    supported set, not a claim that such a reading is handled correctly:
    calling it cumulative would be the worse failure, since a wrongly
    cumulative reading gets summed into Long Term Statistics and frozen by
    _accept_reading() the first time it falls, where the reverse mistake only
    costs the wrong statistics type.
    """
    if canonical in VOLUME_UNITS or canonical in ENERGY_UNITS:
        return True

    lowered = raw_unit.lower()
    return any(marker in lowered for marker in ALLOCATOR_UNIT_MARKERS)


def _device_name(meter_type: str, placement: str | None, meter_id: str) -> str:
    """Name a meter's device.

    Brunata's own UI lets a customer label each meter with its physical
    location ("placement", e.g. "Koldt vand") — when that label is available,
    lead with it so the device is recognisable without opening it; otherwise
    fall back to the generic type+ID name used before placement existed.

    "Brunata" is left out: it is already shown as the device's manufacturer,
    and repeating it in every entity name is redundant clutter in the UI.
    """
    if placement:
        return f"{meter_type} - {placement}"
    return f"{meter_type} ({meter_id})"


def _as_iso(value: date | datetime | str | None) -> str | None:
    """Return a reading date as an ISO string.

    The API hands us date objects, while a state restored after a restart
    yields the string it was serialised as. Normalising here keeps the
    reading_date attribute a single type across restarts, so templates and
    automations reading it don't break on reload.
    """
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _as_date(value: date | datetime | str | None) -> date | None:
    """Return a reading date as a date object, or None if it can't be parsed.

    Used only for the calendar-year comparison in _is_annual_reset(). A state
    restored after a restart hands us the ISO string it was serialised as, so
    it has to be parsed back; anything unparseable simply disables the year
    rule and falls back to the December/January window.

    The string case is api.py's parser, not a second copy of it. What the
    restore store holds is what we serialised from the API's own values, so
    there is one spelling of a Brunata date and one place that knows it. Two
    private copies would have drifted without anything failing, because each
    had its own test.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return _parse_reading_date(value)


@dataclass
class BrunataRestoredData(ExtraStoredData):
    """The part of the decrease guard's baseline that the state cannot carry.

    The cached value survives a restart as the entity's state, and the reading
    date as a state attribute. The meter number and mounting date did not, and
    that broke the replacement rule: __init__ reads both from whatever the
    coordinator holds at the moment the entity is created, so after a
    replacement that happened while Home Assistant was down, both fields
    already hold the *new* meter's values. The restored value is the old high
    one, the incoming value is near zero, and neither field looks changed — so
    the reset is rejected, and rejected again every hour, until the new meter
    passes the old one's final reading. For a water meter that is years.

    Stored in Home Assistant's restore store rather than as state attributes,
    so the meter number stays out of the state machine. diagnostics.py redacts
    it for the same reason: it identifies a physical device at an address.
    """

    meter_no: str | None
    mounting_date: str | None

    def as_dict(self) -> dict[str, Any]:
        """Serialise for the restore store."""
        return {"meter_no": self.meter_no, "mounting_date": self.mounting_date}

    @classmethod
    def from_dict(cls, restored: dict[str, Any]) -> BrunataRestoredData | None:
        """Rebuild from the restore store, or None if it is not ours.

        Returns None rather than raising for anything unexpected: a store
        written by an older version simply has no such keys, and an entity
        that cannot restore its baseline is in exactly the state it was in
        before this class existed.
        """
        if not isinstance(restored, dict):
            return None
        if "meter_no" not in restored or "mounting_date" not in restored:
            return None
        return cls(restored["meter_no"], restored["mounting_date"])


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BrunataConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Brunata sensors based on a config entry."""
    _LOGGER.debug("Setting up Brunata sensors for entry %s", entry.entry_id)
    coordinator = entry.runtime_data

    known_meter_ids: set[str] = set()

    @callback
    def _add_new_meters() -> None:
        """Add sensor entities for any newly discovered meters."""
        new_entities = []
        # `or {}` for the same reason as in _apply_latest_reading(): the two
        # places that read coordinator.data should make the same assumption
        # about it, or the difference reads as if one of them knows something.
        for meter_id, meter in (coordinator.data or {}).items():
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
        # When Brunata installed the physical device. The other half of the
        # replacement signal, and the half that survives a meter number being
        # reused. Both are restored in async_added_to_hass(), because both are
        # read here from the same payload as the reading they are supposed to
        # be compared against.
        self._mounting_date: datetime | None = meter.mounting_date
        self._transmitting: bool | None = meter.transmitting
        # Whether the current run of rejected decreases has already been
        # logged. See _accept_reading().
        self._decrease_warned = False
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
        if raw_unit.lower() == UNDEFINED_UNIT:
            raw_unit = ""
        unit = UNIT_MAP.get(raw_unit.lower())

        if unit is not None:
            self._attr_native_unit_of_measurement = unit
        elif not raw_unit:
            # unit missing from the API response — see FALLBACK_UNIT.
            self._attr_native_unit_of_measurement = FALLBACK_UNIT
        else:
            # Unrecognised unit: pass it through unchanged, but deliberately
            # claim no device class for it — HA would reject the combination.
            self._attr_native_unit_of_measurement = raw_unit

        # Determine the device class from the canonical unit.
        #
        # No icon is set for the two that have one: Home Assistant already
        # draws mdi:water for device_class WATER and mdi:lightning-bolt for
        # ENERGY, so setting them here only froze this integration's sensors
        # to whatever those defaults happened to be. An allocation meter has
        # no device class and therefore no default, so it keeps an explicit
        # icon.
        if unit in VOLUME_UNITS:
            self._attr_device_class = SensorDeviceClass.WATER
        elif unit in ENERGY_UNITS:
            self._attr_device_class = SensorDeviceClass.ENERGY
        else:
            self._attr_icon = "mdi:gauge"

        # Consumption readings climb until the meter is zeroed — every
        # 1 January for a heat cost allocator, and whenever the physical device
        # is replaced for any meter. TOTAL_INCREASING is built for exactly
        # that: it lets HA compute hourly sums and aggregate consumption, and
        # its statistics engine already knows how to handle a drop back to
        # zero.
        #
        # Anything Brunata might report outside water, energy and heat cost
        # allocators is not supported — see _is_cumulative_unit() for why it
        # still gets a safe, non-cumulative default rather than being rejected
        # outright.
        self._cumulative = _is_cumulative_unit(
            unit, self._attr_native_unit_of_measurement or ""
        )
        self._attr_state_class = (
            SensorStateClass.TOTAL_INCREASING
            if self._cumulative
            else SensorStateClass.MEASUREMENT
        )
        # Brunata states the precision it displays itself: 3 digits for
        # water, 0 for heat cost allocators. Using its number avoids guessing
        # per unit and follows automatically if a meter type is added. Display
        # only — native_value and Long Term Statistics keep the full float.
        if meter.decimals is not None:
            self._attr_suggested_display_precision = meter.decimals
        elif unit in VOLUME_UNITS:
            self._attr_suggested_display_precision = 3
        elif unit in ENERGY_UNITS:
            self._attr_suggested_display_precision = 2
        else:
            self._attr_suggested_display_precision = 0

        # Whether this meter is zeroed every 1 January. Decided from the
        # numeric meterType rather than its resolved name, so a relabelled or
        # differently translated lookup table cannot quietly turn the rule off.
        # See ANNUAL_RESET_METER_TYPES and _accept_reading().
        self._resets_annually = meter.meter_type_code in ANNUAL_RESET_METER_TYPES

        # Group under a device per meter. The name is built by _device_name()
        # because _async_update_device_name() has to build the same one when
        # the label changes.
        self._placement = meter.placement
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"brunata_{self._meter_id}")},
            name=_device_name(meter.meter_type, meter.placement, self._meter_id),
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

        The baseline is two halves. The value and the reading date come back
        from the state; the meter number and mounting date come back from the
        extra data, because they are not in the state at all — see
        BrunataRestoredData for what happened when they did not come back.
        """
        await super().async_added_to_hass()

        # Before the value, so that a store written by an older version — which
        # has no extra data — leaves __init__'s values in place and behaves
        # exactly as it did then, for that one restart.
        last_extra = await self.async_get_last_extra_data()
        if last_extra is not None:
            restored_extra = BrunataRestoredData.from_dict(last_extra.as_dict())
            if restored_extra is not None:
                self._meter_no = restored_extra.meter_no
                self._mounting_date = _parse_timestamp(
                    restored_extra.mounting_date
                )

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

    @property
    def extra_restore_state_data(self) -> BrunataRestoredData:
        """Persist the decrease guard's baseline metadata across a restart.

        Home Assistant reads this whenever it writes the entity's state to the
        restore store, so it always describes the meter the cached value came
        from — which is the whole point: it has to be the *old* meter's
        identity that survives, not the one the next payload happens to carry.
        """
        return BrunataRestoredData(
            meter_no=self._meter_no,
            mounting_date=(
                self._mounting_date.isoformat()
                if self._mounting_date is not None
                else None
            ),
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator.

        The accept/reject decision lives here rather than in the
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
        if meter is None:
            # Brunata no longer reports this meter, so nothing here is current
            # any more. The reading is deliberately left alone — it is the last
            # thing the meter really registered, and dropping it would put a
            # hole in the statistics — but `transmitting` is a claim about the
            # meter right now, and "true" on a dismounted meter is simply
            # false. Cleared to None: unknown, which is what it is.
            #
            # `placement` is left as it was. It is a label, not a state, and it
            # keeps the sensor's own attributes readable next to a device name
            # that also still carries it.
            self._transmitting = None
            return

        # Metadata is refreshed whether or not the reading below is accepted:
        # a rejected value says nothing about the meter's label.
        if meter.placement != self._placement:
            self._placement = meter.placement
            self._async_update_device_name(meter)
        self._transmitting = meter.transmitting

        if meter.value is None or not self._accept_reading(meter):
            return

        # A reading got through, so the next rejection is news again.
        self._decrease_warned = False
        self._attr_native_value = meter.value
        self._meter_no = meter.meter_no
        self._mounting_date = meter.mounting_date
        self._last_reading_date = _as_iso(meter.reading_date)
        self._last_reading_day = meter.reading_date

    @callback
    def _async_update_device_name(self, meter: BrunataMeter) -> None:
        """Follow a relabelled meter into the device registry.

        DeviceInfo is only read when the entity is added, so without this a
        meter renamed in Brunata's own UI keeps its old device name until the
        config entry is reloaded — while the placement attribute updates on the
        next poll. That split is what a user notices: the attribute says
        "Kitchen" and the device is still called "Water - Living room".

        Only `name` is written. A name the user typed in Home Assistant lands
        in `name_by_user`, which the UI prefers and which this leaves alone.
        """
        if self.hass is None:
            return

        name = _device_name(meter.meter_type, meter.placement, self._meter_id)
        device_registry = dr.async_get(self.hass)
        device = device_registry.async_get_device(
            identifiers={(DOMAIN, f"brunata_{self._meter_id}")}
        )
        if device is None or device.name == name:
            return

        _LOGGER.debug(
            "Meter %s was relabelled: renaming device %r to %r",
            self._meter_id,
            device.name,
            name,
        )
        device_registry.async_update_device(device.id, name=name)

    def _accept_reading(self, meter: BrunataMeter) -> bool:
        """Decide whether a reading should replace the cached value.

        An increase is always accepted. A decrease is believed in exactly two
        cases, both of which Brunata states outright:

        1. The mounting date or the meter number changed. The physical device
           was replaced, so the new one legitimately starts near zero —
           whatever its type, whatever the date.
        2. It is a heat cost allocator around new year. Those are zeroed on
           1 January, every year, without exception.

        Anything else is discarded as a glitch: accepting one under
        TOTAL_INCREASING would make Home Assistant record a false consumption
        spike on the way back up.

        This replaced a heuristic that adopted any decrease seen across three
        separate reading dates. That was a stand-in for the replacement signal
        we now get directly from mountingDate, and it could be fooled by a
        sustained API fault. The trade-off: a reset Brunata reports without
        touching either the mounting date or the meter number is now rejected
        indefinitely. No such case has been observed, and the warning below
        makes it visible if one appears.
        """
        previous = self._attr_native_value
        value = meter.value

        if previous is None or value >= previous:
            return True

        if not self._cumulative:
            # A non-cumulative reading is not reporting a reset by falling —
            # holding its old value would freeze it at the highest reading it
            # ever took. Only meters whose readings accumulate get a guard at
            # all — see _is_cumulative_unit().
            return True

        if (
            meter.mounting_date != self._mounting_date
            or meter.meter_no != self._meter_no
        ):
            _LOGGER.info(
                "Meter %s was replaced (meter number %s -> %s, mounted %s -> "
                "%s): accepting the reset from %s to %s",
                self._meter_id,
                self._meter_no,
                meter.meter_no,
                self._mounting_date,
                meter.mounting_date,
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

        # Logged once per run of rejections, not once per poll. The cached
        # value is never lowered, so a decrease that is rejected once is
        # rejected again every hour for as long as it persists — potentially
        # forever. Twenty-four identical lines a day bury everything else in
        # the log without adding anything to the first one. The flag is
        # cleared in _apply_latest_reading() as soon as a reading is accepted.
        if not self._decrease_warned:
            self._decrease_warned = True
            _LOGGER.warning(
                "Meter %s reported a decrease (%s -> %s on %s) that is neither "
                "a replacement nor an annual reset — keeping the previous "
                "value. If the meter really was reset, check it in Brunata "
                "Online. Further rejections will not be logged until a reading "
                "is accepted again.",
                self._meter_id,
                previous,
                value,
                meter.reading_date,
            )
        return False

    def _is_annual_reset(self, reading_date: date | None) -> bool:
        """Return True if a decrease on this date is the annual 1 January reset.

        Heat cost allocators are zeroed on 1 January, but the first reading
        Brunata publishes afterwards is not necessarily dated 1 January — these
        meters report infrequently, so it can arrive dated several days or
        weeks into the new year. Matching only (12, 31) and (1, 1) meant such a
        reading was rejected as a glitch, and because the cached value is never
        lowered, every reading for the rest of the year was rejected too: the
        sensor froze at the pre-reset value until the new period happened to
        exceed it.

        The reliable signal is therefore the calendar year: a reading dated in
        a later year than the last accepted one is on the far side of a
        1 January, whenever it happens to arrive.

        The December/January window is only a fallback for when the previous
        reading date is unknown — a state restored from before that attribute
        was recorded, or a reading that arrived without a parseable date. It
        used to be checked either way, which made it wider than it was meant to
        be: a glitch on 20 January, with the last accepted reading dated
        12 January of the *same* year, was adopted as an annual reset even
        though no year boundary had been crossed.

        A decrease with no usable date at all cannot be placed in the calendar,
        so it is not a reset either.
        """
        if reading_date is None:
            return False

        last_day = self._last_reading_day
        if last_day is not None:
            return reading_date.year > last_day.year

        return reading_date.month == 1 or (reading_date.month, reading_date.day) == (
            12,
            31,
        )

    @property
    def available(self) -> bool:
        """Available while Brunata still reports this meter.

        A dismounted meter is dropped from the payload, so it disappears from
        the coordinator's data. Reporting it as unavailable is the honest
        answer — the alternative is a device sitting in Home Assistant showing
        a final reading forever, indistinguishable from a working one.

        A failed update is deliberately not a reason to go unavailable: the
        coordinator keeps the previous data, so the meter is still present and
        the sensor keeps its value. The reading_date attribute is what tells
        you how fresh that value is.
        """
        if self._attr_native_value is None:
            return False
        data = self.coordinator.data
        return data is None or self._meter_id in data

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the state attributes."""
        attributes: dict[str, Any] = {}
        if self._last_reading_date is not None:
            attributes["reading_date"] = self._last_reading_date
        if self._placement is not None:
            attributes["placement"] = self._placement
        if self._transmitting is not None:
            attributes["transmitting"] = self._transmitting
        return attributes

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
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import BrunataConfigEntry, BrunataDataUpdateCoordinator
from .api import (
    BASE_URL,
    BrunataMeter,
    format_date,
    parse_reading_date,
    parse_timestamp,
)
from .const import DEVICE_ID_PREFIX, DOMAIN

_LOGGER = logging.getLogger(__name__)

# Meter types Brunata zeroes on a schedule: heat cost allocators, every
# 1 January. A decrease around new year therefore needs no further evidence.
#
# Matched on the numeric meterType, not the name it resolves to. The name comes
# from a locale table Brunata owns, fetched in whatever language LOCALE asks
# for, so matching "radiator" in it was a rule that would switch itself off
# silently the day Brunata relabelled the entry — and since the cached value is
# never lowered, the 1 January decrease would then be rejected as a glitch and
# every reading for the rest of the year rejected with it.
#
# Not a list of the only meters that can drop: *any* meter starts from zero
# when the physical device is replaced, at any time of year.
# _accept_reading() recognises those.
#
#   1 = heat cost allocator (radiator). Read off live account data.
ANNUAL_RESET_METER_TYPES = frozenset({1})


# Keys are entries in Brunata's measurementUnit table, lowercased and stripped,
# because the casing is not guaranteed ("kWh", "KWH", "l", "L", "m3", "m³").
# Values are Home Assistant's canonical constants: a device class combined with
# a non-canonical unit string is rejected by HA's unit validation, which logs an
# error and discards the entity's long term statistics.
#
# That table has 96 slots and spans far more than metering. Only entries with a
# Home Assistant equivalent are mapped; everything else passes through verbatim
# and gets no device class — the correct outcome, not a limitation. See
# _is_cumulative_unit() for how such a reading is still handled safely.
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
    # Deliberately absent: "Btu" has no Home Assistant equivalent, and the rest
    # of Brunata's table is outside what this integration is built for. What
    # state class an unmapped reading gets is decided by _is_cumulative_unit().
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

# Substrings that identify an allocator unit — a count that only ever climbs.
# Brunata's table carries a dozen vendor-specific variants ("Doprimo units",
# "Zenner units"), so matching the marker rather than listing the spellings
# keeps a new vendor from being misclassified.
#
# There is deliberately no fallback unit and no special case for the table's
# "undefined" entry. Both used to stand in for a unit Brunata had not given us,
# producing a plausible-looking sensor carrying a unit nobody had read — which
# Home Assistant treats as a different measurement, discarding the history
# behind it. api.py drops such a meter instead, so anything reaching this
# module has a unit that resolved.
ALLOCATOR_UNIT_MARKERS = ("unit", "pts")


def _is_cumulative_unit(canonical: str | None, raw_unit: str) -> bool:
    """Return True if readings in this unit only ever climb.

    The one decision the state class and the decrease guard both hang off, so
    it is written once. Volume, energy and allocator units all count up until
    the meter is zeroed; this integration is built for those three.

    An unrecognised unit is treated as *not* cumulative — a defensive default,
    not a claim that such a reading is handled correctly. Calling it cumulative
    would be the worse failure: it would be summed into Long Term Statistics
    and frozen by _accept_reading() the first time it fell, where the reverse
    mistake only costs the wrong statistics type.
    """
    if canonical in VOLUME_UNITS or canonical in ENERGY_UNITS:
        return True

    lowered = raw_unit.lower()
    return any(marker in lowered for marker in ALLOCATOR_UNIT_MARKERS)


def _device_name(meter_type: str, placement: str | None, meter_id: str) -> str:
    """Name a meter's device.

    Brunata's UI lets a customer label each meter with its physical location
    ("placement", e.g. "Koldt vand"). Lead with it when present so the device
    is recognisable without opening it; otherwise fall back to type+ID.

    "Brunata" is left out: it is already the device's manufacturer, and
    repeating it in every entity name is clutter.
    """
    if placement:
        return f"{meter_type} - {placement}"
    return f"{meter_type} ({meter_id})"


def _as_date(value: date | datetime | str | None) -> date | None:
    """Return a reading date as a date object, or None if it can't be parsed.

    Used only for the calendar-year comparison in _is_annual_reset(). A
    restored state hands us the ISO string it was serialised as, so it has to
    be parsed back; anything unparseable disables the year rule and falls back
    to the December/January window.

    The string case is api.py's parser, not a second copy of it: the restore
    store holds what we serialised from the API's own values, so there is one
    spelling of a Brunata date and one place that knows it. api.format_date()
    is the other direction of the same rule.

    datetime is checked *first* here, and that order is load-bearing: datetime
    subclasses date, so the two branches would otherwise collapse into one and
    a datetime would be returned where a date was promised.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return parse_reading_date(value)


@dataclass
class BrunataRestoredData(ExtraStoredData):
    """The part of the decrease guard's baseline that the state cannot carry.

    The cached value survives a restart as the entity's state, and the reading
    date as a state attribute. The meter number and mounting date did not, and
    that broke the replacement rule: __init__ reads both from whatever the
    coordinator holds when the entity is created, so after a replacement that
    happened while Home Assistant was down, both already hold the *new* meter's
    values. Neither field looks changed, so the reset is rejected — and
    rejected again every hour until the new meter passes the old one's final
    reading. For a water meter that is years.

    Stored in the restore store rather than as state attributes, so the meter
    number stays out of the state machine. diagnostics.py redacts it for the
    same reason: it identifies a physical device at an address.
    """

    meter_no: str | None
    mounting_date: str | None

    def as_dict(self) -> dict[str, Any]:
        """Serialise for the restore store."""
        return {"meter_no": self.meter_no, "mounting_date": self.mounting_date}

    @classmethod
    def from_dict(cls, restored: dict[str, Any]) -> BrunataRestoredData | None:
        """Rebuild from the restore store, or None if it is not ours.

        None rather than raising: a store written by an older version simply
        has no such keys, and an entity that cannot restore its baseline is in
        exactly the state it was in before this class existed.
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

    # Held on the coordinator, not in a closure here, so
    # async_remove_config_entry_device() can take an id back out when the user
    # deletes a dismounted meter's device. Otherwise the id stayed for the life
    # of the entry and the entity could never be built again.
    known_meter_ids = coordinator.known_meter_ids

    @callback
    def _add_new_meters() -> None:
        """Add sensor entities for any newly discovered meters."""
        new_entities = []
        # `or {}` as in _apply_latest_reading(): the two places that read
        # coordinator.data should make the same assumption about it.
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
        # when a device is swapped, so a change here means the hardware was
        # replaced and the new device counts from zero. mounting_date is the
        # other half of that signal, and the half that survives a meter number
        # being reused. Both are restored in async_added_to_hass(), because
        # both are read here from the same payload as the reading they are
        # supposed to be compared against.
        self._meter_no = meter.meter_no
        self._mounting_date: datetime | None = meter.mounting_date
        self._transmitting: bool | None = meter.transmitting
        # Whether the current run of rejected decreases has been logged. See
        # _accept_reading().
        self._decrease_warned = False
        # Cache the last known good reading so the sensor keeps its value —
        # and stays available — between the infrequent API updates instead of
        # going unavailable, which would break statistics rows. Updated only
        # from _apply_latest_reading(), never from a property.
        self._attr_native_value = None
        # Kept as an ISO string for the state attribute, and as a date object
        # for the year comparison in _is_annual_reset().
        self._last_reading_date: str | None = None
        self._last_reading_day: date | None = None
        self._attr_has_entity_name = True
        self._attr_translation_key = "consumption"

        # api.py has already resolved the unit against Brunata's table and
        # dropped the meter if it could not, so this is a real unit name.
        raw_unit = meter.unit.strip()
        unit = UNIT_MAP.get(raw_unit.lower())

        if unit is not None:
            self._attr_native_unit_of_measurement = unit
        else:
            # A unit Home Assistant has no constant for — allocator units are
            # the everyday case. Passed through as Brunata spells it, and
            # deliberately claiming no device class: HA rejects a device class
            # combined with a unit it does not recognise.
            self._attr_native_unit_of_measurement = raw_unit

        # No icon for the two that have one: Home Assistant already draws
        # mdi:water for device_class WATER and mdi:lightning-bolt for ENERGY,
        # so setting them here only froze this integration's sensors to
        # whatever those defaults happened to be. An allocation meter has no
        # device class and therefore no default, so it keeps an explicit icon.
        if unit in VOLUME_UNITS:
            self._attr_device_class = SensorDeviceClass.WATER
        elif unit in ENERGY_UNITS:
            self._attr_device_class = SensorDeviceClass.ENERGY
        else:
            self._attr_icon = "mdi:gauge"

        # Consumption readings climb until the meter is zeroed — every
        # 1 January for a heat cost allocator, and whenever the physical device
        # is replaced for any meter. TOTAL_INCREASING is built for exactly
        # that: HA computes hourly sums and already knows how to handle a drop
        # back to zero. Anything outside water, energy and heat cost allocators
        # gets a safe non-cumulative default — see _is_cumulative_unit().
        self._cumulative = _is_cumulative_unit(
            unit, self._attr_native_unit_of_measurement or ""
        )
        self._attr_state_class = (
            SensorStateClass.TOTAL_INCREASING
            if self._cumulative
            else SensorStateClass.MEASUREMENT
        )
        # Brunata states the precision it displays itself: 3 digits for water,
        # 0 for heat cost allocators. Using its number avoids guessing per unit
        # and follows automatically if a meter type is added. Display only —
        # native_value and Long Term Statistics keep the full float.
        if meter.decimals is not None:
            self._attr_suggested_display_precision = meter.decimals
        elif unit in VOLUME_UNITS:
            self._attr_suggested_display_precision = 3
        elif unit in ENERGY_UNITS:
            self._attr_suggested_display_precision = 2
        else:
            self._attr_suggested_display_precision = 0

        # Decided from the numeric meterType rather than its resolved name, so
        # a relabelled or differently translated table cannot quietly turn the
        # rule off. See ANNUAL_RESET_METER_TYPES and _accept_reading().
        self._resets_annually = meter.meter_type_code in ANNUAL_RESET_METER_TYPES

        # One device per meter. The name is kept because _apply_latest_reading()
        # compares the name it builds from each payload against this one to
        # decide whether the device registry needs updating.
        self._placement = meter.placement
        self._device_name = _device_name(
            meter.meter_type, meter.placement, self._meter_id
        )
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, f"{DEVICE_ID_PREFIX}{self._meter_id}")},
            name=self._device_name,
            manufacturer="Brunata",
            model=meter.meter_type,
            # Brunata's portal is where a user relabels a meter — the change
            # this integration then follows into the device registry — so the
            # device page links straight to it. The account root rather than
            # the readings page: REFERER_URL is a request header and reusing it
            # here would overload what that constant means.
            configuration_url=BASE_URL,
            # Shown on the device page. Redacted in diagnostics.py, and that is
            # not a contradiction: the redaction is about publishing the number
            # in a public issue, not about showing an owner their own meter.
            serial_number=meter.meter_no,
        )
        _LOGGER.debug(
            "Initialized BrunataSensor for meter %s (%s)",
            self._meter_id,
            meter.meter_type,
        )

    async def async_added_to_hass(self) -> None:
        """Restore the last known reading across restarts and reloads.

        The cached value and reading date are plain in-memory attributes, so
        they reset whenever this entity is recreated. available() would then
        depend on the coordinator already holding a fresh reading for this
        meter at that exact moment. Restoring closes that gap and gives the
        decrease guard its baseline.

        The baseline is two halves: the value and reading date come from the
        state, the meter number and mounting date from the extra data, because
        they are not in the state at all. See BrunataRestoredData.
        """
        await super().async_added_to_hass()

        # Before the value, so a store written by an older version — which has
        # no extra data — leaves __init__'s values in place for that one
        # restart and behaves exactly as it did then.
        last_extra = await self.async_get_last_extra_data()
        if last_extra is not None:
            restored_extra = BrunataRestoredData.from_dict(last_extra.as_dict())
            if restored_extra is not None:
                self._meter_no = restored_extra.meter_no
                self._mounting_date = parse_timestamp(
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
                self._last_reading_date = format_date(restored_date)
                self._last_reading_day = _as_date(restored_date)

        # Apply whatever the coordinator already holds, now that the restored
        # value is in place as the baseline.
        self._apply_latest_reading()

    @property
    def extra_restore_state_data(self) -> BrunataRestoredData:
        """Persist the decrease guard's baseline metadata across a restart.

        Read whenever Home Assistant writes the entity's state, so it always
        describes the meter the cached value came from — which is the point: it
        has to be the *old* meter's identity that survives, not whatever the
        next payload carries.
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

        The accept/reject decision lives here rather than in the native_value
        property. A property is read on every state write, in an order not
        guaranteed relative to available() and extra_state_attributes, so
        mutating state from inside one made the reset logic run an
        unpredictable number of times per update.
        """
        self._apply_latest_reading()
        super()._handle_coordinator_update()

    @callback
    def _apply_latest_reading(self) -> None:
        """Fold the coordinator's latest reading into the cached state.

        A fresh reading can arrive hourly or as rarely as once a day. When
        there is none, the cached value is left alone, so the sensor never goes
        unknown/unavailable and statistics stay intact.
        """
        meter = (self.coordinator.data or {}).get(self._meter_id)
        if meter is None:
            # Brunata no longer reports this meter. The reading is deliberately
            # kept — it is the last thing the meter really registered, and
            # dropping it would put a hole in the statistics — but
            # `transmitting` is a claim about right now, and "true" on a
            # dismounted meter is simply false. Cleared to None: unknown.
            #
            # `placement` is left as it was. It is a label, not a state.
            self._transmitting = None
            return

        # Metadata is refreshed whether or not the reading below is accepted:
        # a rejected value says nothing about the meter's label.
        self._placement = meter.placement
        self._transmitting = meter.transmitting

        # Compared on the built name rather than on placement alone. The device
        # name is placement *and* meter type, so gating on the label would let
        # a changed type sit unnoticed until the entry was reloaded; and
        # comparing here rather than inside the function below keeps the common
        # case free of a device registry lookup.
        name = _device_name(meter.meter_type, meter.placement, self._meter_id)
        if name != self._device_name:
            self._device_name = name
            self._async_update_device_name(name)

        if meter.value is None or not self._accept_reading(meter):
            return

        # A reading got through, so the next rejection is news again.
        self._decrease_warned = False
        self._attr_native_value = meter.value
        self._meter_no = meter.meter_no
        self._mounting_date = meter.mounting_date

        # Only when the reading carries a date. An accepted reading without one
        # used to overwrite both fields with None, and _is_annual_reset() reads
        # a missing _last_reading_day as "no baseline", switching back to the
        # December/January window the calendar-year rule replaced. One undated
        # reading therefore reopened that window for the rest of the December
        # and January it fell in, and a glitch dated 31 December was then
        # adopted as an annual reset.
        #
        # The cost is that reading_date describes the last reading Brunata
        # dated rather than the value now shown — the smaller of the two, since
        # an attribute that lags is visible and a widened guard is not.
        if meter.reading_date is not None:
            self._last_reading_date = format_date(meter.reading_date)
            self._last_reading_day = meter.reading_date

    @callback
    def _async_update_device_name(self, name: str) -> None:
        """Follow a renamed meter into the device registry.

        DeviceInfo is only read when the entity is added, so without this a
        meter renamed in Brunata's own UI keeps its old device name until the
        entry is reloaded — while the placement attribute updates on the next
        poll. That split is what a user notices: the attribute says "Kitchen"
        and the device is still called "Water - Living room".

        Takes the finished name rather than the meter, because the caller has
        already built it in order to notice that it changed. That is also why
        there is no name comparison here: the caller only calls this when the
        name it holds has changed, so a second check would answer the same
        question twice.

        Only `name` is written. A name the user typed in Home Assistant lands
        in `name_by_user`, which the UI prefers and which this leaves alone.
        """
        if self.hass is None:
            return

        device_registry = dr.async_get(self.hass)
        device = device_registry.async_get_device(
            identifiers={(DOMAIN, f"{DEVICE_ID_PREFIX}{self._meter_id}")}
        )
        if device is None:
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

        1. The mounting date or the meter number changed — the physical device
           was replaced, so the new one legitimately starts near zero. This
           applies to *every* meter that counts upwards. All meters are
           replaced eventually, roughly every 8-10 years, heat cost allocators
           included; the check below is deliberately not conditioned on meter
           type, and it is tested first.
        2. It is a heat cost allocator around new year. Those are zeroed on
           1 January, every year. This one is on top of case 1 for allocators,
           not instead of it.

        Anything else is discarded as a glitch: accepting one under
        TOTAL_INCREASING would record a false consumption spike on the way up.

        This replaced a heuristic that adopted any decrease seen across three
        reading dates — a stand-in for the replacement signal mountingDate now
        gives us directly, and one a sustained API fault could fool. The
        trade-off: a reset Brunata reports without touching either field is now
        rejected indefinitely. No such case has been observed, and the warning
        below makes it visible if one appears.
        """
        previous = self._attr_native_value
        value = meter.value

        if previous is None or value >= previous:
            return True

        if not self._cumulative:
            # A non-cumulative reading is not reporting a reset by falling;
            # holding its old value would freeze it at its highest ever
            # reading. Only accumulating meters get a guard at all.
            return True

        number_changed = meter.meter_no != self._meter_no
        mounting_changed = meter.mounting_date != self._mounting_date
        if number_changed or mounting_changed:
            # The meter numbers themselves are deliberately not in this line.
            # They identify a physical device at an address, which is why
            # diagnostics.py redacts the field and why bug_report.yml tells
            # users the numbers have been removed before the file is written —
            # and a debug log is the other attachment that template asks for.
            # What makes the line worth reading is *which* signal fired, and
            # that survives without printing either number.
            signals = []
            if number_changed:
                signals.append("meter number changed")
            if mounting_changed:
                signals.append(
                    f"mounting date {self._mounting_date} -> {meter.mounting_date}"
                )
            _LOGGER.info(
                "Meter %s was replaced (%s): accepting the reset from %s to %s",
                self._meter_id,
                ", ".join(signals),
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
        # value is never lowered, so a rejected decrease is rejected again
        # every hour for as long as it persists — potentially forever. The flag
        # is cleared in _apply_latest_reading() when a reading is accepted.
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
        published afterwards is not necessarily dated 1 January. Matching only
        (12, 31) and (1, 1) rejected such a reading as a glitch, and since the
        cached value is never lowered, every reading for the rest of the year
        was rejected with it: the sensor froze at the pre-reset value until the
        new period happened to exceed it.

        An earlier version of this docstring explained the gap by saying these
        meters report infrequently, so a reset could be weeks in arriving. That
        was never read anywhere. Brunata's own reading list shows one reading
        per meter per day, around 02:00, whether or not the value moved, so a
        reset is visible within a day. The year comparison below does not
        depend on which of the two is true — it is only the explanation that
        was invented, and an invented explanation is what someone changes the
        rule on later.

        The reliable signal is the calendar year: a reading dated in a later
        year than the last accepted one is on the far side of a 1 January,
        whenever it arrives.

        The December/January window is only a fallback for when the previous
        reading date is unknown. It used to be checked either way, which made
        it wider than intended: a glitch on 20 January, with the last accepted
        reading dated 12 January of the *same* year, was adopted as a reset
        even though no year boundary had been crossed.

        A decrease with no usable date cannot be placed in the calendar, so it
        is not a reset either.
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
        the coordinator's data. Reporting it unavailable is the honest answer;
        the alternative is a device showing a final reading forever,
        indistinguishable from a working one.

        A failed update is deliberately *not* a reason to go unavailable: the
        coordinator keeps the previous data, so the meter is still present and
        the sensor keeps its value. reading_date is what tells you how fresh
        that value is.

        Absence from the payload is not the same as being gone, though. api.py
        also drops a meter whose unit it could not name this poll — rightly,
        because an entity carrying a raw code as its unit loses its statistics
        permanently — but that meter is still on the wall. Going unavailable
        for it would punch exactly the hole in Long Term Statistics that the
        paragraph above refuses to punch for a failed update, and for the same
        kind of transient cause. Those ids are carried in
        coordinator.last_parse; see ParseReport.
        """
        if self._attr_native_value is None:
            return False
        data = self.coordinator.data
        if data is None or self._meter_id in data:
            return True
        return self._meter_id in self.coordinator.last_parse.unresolved_unit_meter_ids

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

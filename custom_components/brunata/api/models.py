"""The objects the API package hands to the rest of the integration.

Plain data with no behaviour and no imports from the rest of the package: a
meter, what a parse produced, and what it saw while producing it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import NamedTuple

@dataclass(frozen=True, slots=True)
class BrunataMeter:
    """A single meter and its most recent reading.

    Flat by design: every field comes straight from one entry in the
    /consumer/metersforconsumer payload, so an absent reading is simply
    ``value is None`` rather than a missing nested object.
    """

    meter_id: str
    meter_no: str | None
    # Resolved through the locale lookup table, e.g. "Radiator". A translation
    # Brunata owns, so it belongs in device names and nowhere a decision is
    # made — see meter_type_code.
    meter_type: str
    # The resolved unit name, e.g. "m3" or "units". Never a raw code and never
    # empty: _parse_meters() drops a meter whose unit it cannot name, because
    # an entity created with the wrong unit loses its history permanently and
    # a skipped one does not.
    unit: str
    # The raw meterType, already checked against SUPPORTED_METER_TYPES. This is
    # the stable half of the pair: sensor.py decides whether a meter is zeroed
    # every 1 January from this number, not from the name above, which would
    # silently stop matching if Brunata relabelled the entry.
    #
    # Defaults to None because that is what _meter_type_code() returns for a
    # value it cannot check, and every rule reading it must treat "unknown" the
    # way the allowlist does: as not one of ours.
    meter_type_code: int | None = None
    value: float | None = None
    # The day of the reading. Every rule in sensor.py that compares readings
    # works on this, and so does the reading_date attribute.
    reading_date: date | None = None
    # The same latestReadingDate with its time and offset kept, e.g.
    # 2026-10-11T02:13:00+02:00. Shown by the "Latest reading" timestamp
    # sensor and used for nothing else. None unless the offset was there:
    # Home Assistant rejects a timestamp sensor value without a time zone.
    reading_time: datetime | None = None
    # The customer's own label from Brunata's UI, e.g. "Koldt vand".
    placement: str | None = None
    # When Brunata installed the physical device. A change is a replacement
    # stated as fact, rather than inferred from a falling value.
    mounting_date: datetime | None = None
    # Number of decimals as Brunata states it: 3 for water, 0 for heat cost
    # allocators. The 0 does not match allocator readings, which carry one
    # decimal, so BrunataSensor shows those with one regardless.
    decimals: int | None = None
    # Whether the meter is currently sending readings.
    transmitting: bool | None = None
    # Brunata's allocationUnit, e.g. "O" for heat cost allocators and "K"/"W"
    # for cold and hot water. Not shown anywhere: it is the one parameter
    # /consumer/consumption needs to select a meter's group, and the call does
    # not take a meter id. See async_get_daily_consumption().
    allocation_unit: str | None = None


class ParseReport(NamedTuple):
    """What the last parse saw, beyond the meters it produced.

    The meters dictionary alone cannot answer "is this meter gone?". A meter
    can be absent from it for three reasons that mean different things:

    * Brunata dismounted it — it really is gone.
    * Its meterType is not one we surface — it never had an entity.
    * Its unit could not be resolved this poll — it is still on the wall, and
      we simply could not name what it measures.

    The third case must not be mistaken for the first, and neither must "the
    payload was empty, so we learned nothing at all". This record carries the
    two facts needed to tell them apart.
    """

    # Meter ids skipped because their unit code did not resolve. Absent from
    # the meters dictionary, but still reported by Brunata.
    unresolved_unit_meter_ids: frozenset[str]
    # How many entries the payload held before any filtering. Zero means
    # Brunata reported nothing, which is not evidence about any single meter.
    raw_item_count: int


class ParsedMeters(NamedTuple):
    """What _parse_meters() returns: the meters, and what it saw getting them."""

    meters: dict[str, BrunataMeter]
    report: ParseReport

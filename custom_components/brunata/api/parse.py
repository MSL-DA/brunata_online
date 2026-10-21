"""Turning Brunata's payloads into the objects in models.py.

Two payloads are parsed here — the meter list and the daily consumption
figures — along with the lookup-table resolution and the date handling both
need. Three of the date helpers are public because sensor.py and
diagnostics.py call them: there is one spelling of a Brunata date and one
module that knows it.

Nothing here touches the network, so every function can be called with a
payload and checked on its own.
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime
from functools import lru_cache
from typing import Any

from .const import ISSUE_TRACKER_URL, SUPPORTED_METER_TYPES, UNDEFINED_UNIT
from .errors import BrunataApiError
from .models import BrunataMeter, ParsedMeters, ParseReport

_LOGGER = logging.getLogger(__name__)

@lru_cache(maxsize=64)
def _log_unresolved_unit(meter_id: str, code: str) -> None:
    """Note a meter skipped for an unusable unit, once per process.

    Cached for the same reason as _log_unsupported_meter(): this runs per
    meter per poll, and the lookup tables do not change between them.

    The wording is deliberate. What the user sees is a sensor that stopped
    updating, so the line has to say the data is safe and nothing needs doing.
    """
    _LOGGER.warning(
        "Meter %s reports unit code %s, which cannot be resolved to a unit, so "
        "this meter is skipped for now. Its sensor stops updating and keeps "
        "the history it already has; it comes back on its own as soon as the "
        "unit resolves again. Creating it with the raw code as its unit would "
        "make Home Assistant treat it as a different measurement and discard "
        "that history, which cannot be undone. If this persists, please report "
        "it with this line at %s.",
        meter_id,
        code,
        ISSUE_TRACKER_URL,
    )


def _meter_type_code(raw: Any) -> int | None:
    """Coerce Brunata's meterType to an int, or None if it isn't one.

    Observed as an integer, but `unit` in the same payload is a string where it
    used to be an integer, so the same could happen here. None means "cannot be
    checked against the allowlist", which SUPPORTED_METER_TYPES treats as "do
    not surface".
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        try:
            return int(raw.strip())
        except ValueError:
            return None
    return None


@lru_cache(maxsize=64)
def _log_unsupported_meter(meter_id: str, code: str) -> None:
    """Note a skipped meter once per process, not once per poll.

    `code` is pre-formatted by the caller: lru_cache hashes its arguments, and
    a meterType arriving as a dict or list is unhashable. Passing it raw threw
    TypeError out of _parse_meters(), costing every meter that update — the
    exact failure this filter exists to avoid.
    """
    _LOGGER.info(
        "Meter %s has meterType %s, which this integration does not support, "
        "so no entity is created for it. Supported types are %s. If this "
        "meter measures consumption you expected to see in Home Assistant, "
        "please report it at %s with this line and what the meter physically "
        "measures — the log can give the code, but only you can say what the "
        "box on the wall is.",
        meter_id,
        code,
        sorted(SUPPORTED_METER_TYPES),
        ISSUE_TRACKER_URL,
    )


def _as_text(raw: Any) -> str:
    """Coerce a lookup code to text.

    The codes have been observed as both integers and strings, so they are
    normalised before being parsed, and an absent code becomes "" rather than
    the string "None". There is no branch for a code that is already a string:
    str() returns one unchanged.
    """
    if raw is None:
        return ""
    return str(raw)


@lru_cache(maxsize=64)
def _warn_unresolved_code(what: str, code: str, table_size: int) -> None:
    """Warn about an unresolved lookup code, once per process.

    _resolve() runs per meter per poll, so without the cache an unresolvable
    code means 24 identical lines a day. The tables are static and
    account-independent, so the second warning carries nothing the first did.

    It says only that the code did not resolve. What happens next differs by
    field — the meter type falls back, the unit skips the meter — so the
    callers say that part.
    """
    _LOGGER.warning(
        "Brunata %s code %r does not resolve in the lookup table (%s entries).",
        what,
        code,
        table_size,
    )


def _resolve(table: list[str], raw: Any, what: str) -> str | None:
    """Resolve a numeric code against one of the locale lookup tables.

    The payload carries indices, not names: meterType 2 means whatever sits at
    index 2 of the meterType table. Returns None when the table cannot answer
    and leaves it to the caller to decide what that is worth — the two fields
    this serves do not deserve the same answer. See _lookup() and
    _parse_meters().
    """
    code = _as_text(raw)
    if not code:
        return None

    try:
        index = int(code)
        # Python indexes lists from both ends and Brunata does not. Without
        # this, -1 returns the table's *last* entry rather than raising: a
        # wrong but perfectly valid unit, with the wrong device class, the
        # wrong state class, and statistics that look right and are not.
        if index < 0:
            raise IndexError(index)
        name = table[index]
    except (ValueError, IndexError):
        name = None

    # The live tables contain null entries — 7 of 28 meter types and 34 of 96
    # units are reserved slots Brunata has not filled in. An index landing on
    # one means the same as an index past the end.
    if not isinstance(name, str) or not name.strip():
        _warn_unresolved_code(what, code, len(table))
        return None

    # Some live entries carry a trailing space, which would end up in the
    # device name.
    return name.strip()


def _lookup(table: list[str], raw: Any, what: str) -> str:
    """Resolve a code, falling back to the raw code when the table cannot.

    Used for the meter type only, where the fallback is deliberate: the name
    reaches the device name and the `model` field and nothing else, since every
    decision hangs on meter_type_code. A device called "2" is ugly, visible and
    fixes itself when the table resolves again.

    The unit gets no fallback — see _parse_meters(). That asymmetry is the
    point: one field is cosmetic when it goes wrong, the other costs the meter
    its history.
    """
    return _resolve(table, raw, what) or _as_text(raw)


def parse_timestamp(raw: Any) -> datetime | None:
    """Parse one of Brunata's ISO timestamps, e.g. mountingDate.

    They carry an offset ("2018-10-23T14:09:22+02:00"), so the result is
    timezone-aware. Unparseable becomes None rather than raising: a meter with
    an odd date is still a meter.

    Public rather than underscored because sensor.py calls it. The restore
    store holds what this module serialised, so there is one spelling of a
    Brunata date and one place that knows it.
    """
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            _LOGGER.debug("Could not parse timestamp %r", raw)
    return None


def _aware_or_none(value: datetime | None) -> datetime | None:
    """Return a timestamp only if it carries a time zone.

    Brunata's timestamps have always carried an offset. One without would be
    read as naive, and Home Assistant raises on a naive value in a timestamp
    sensor, so it is dropped here rather than guessed at.
    """
    if value is None or value.tzinfo is None:
        return None
    return value


def _parse_value(raw: Any) -> float | None:
    """Parse a meter reading, tolerating anything that is not a number.

    Every other field here degrades to None rather than raising, so one odd row
    costs one meter. A bare float() would break that: the ValueError escapes
    _parse_meters() untranslated and reaches the coordinator as an unexpected
    exception, costing every meter that update.

    float() is not the whole guard, though. It also accepts "NaN", "Infinity"
    and "1e400", and none of those is a meter reading. nan is the worse of the
    two: every comparison against it is false, so _accept_reading() takes it
    whenever there is no previous value — first poll after setup, or after a
    restart with nothing to restore — and it becomes the sensor's state.

    Bools are refused for the same reason _meter_type_code() refuses them:
    float(True) is 1.0 and perfectly finite, so a `true` in the payload would
    pass every check below and become a reading of one.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        _LOGGER.debug("Could not parse reading value %r", raw)
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        _LOGGER.debug("Could not parse reading value %r", raw)
        return None
    if not math.isfinite(value):
        _LOGGER.debug("Ignoring non-finite reading value %r", raw)
        return None
    return value


def parse_reading_date(raw: Any) -> date | None:
    """Parse Brunata's reading date, tolerating a full timestamp.

    Public for the same reason as parse_timestamp(): sensor._as_date() hands it
    the string it read back out of the restore store.
    """
    if isinstance(raw, str):
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            _LOGGER.debug("Could not parse reading date %r", raw)
    return None


def format_date(value: Any) -> Any:
    """Render a date or datetime as an ISO string, leaving anything else alone.

    The counterpart to the two parsers above, and public for the same reason:
    sensor.py needs it for the reading_date attribute and diagnostics.py for
    the downloaded report. It lived in both of those files as a private copy
    with the same body and the same docstring note — the 1.3.1 round put date
    *parsing* in one place on the argument that there should be one spelling of
    a Brunata date and one module that knows it, and this is the other half of
    that argument.

    Passing non-dates through unchanged is what both callers want. A reading
    date restored from the state machine is already the ISO string we wrote,
    and every other field in a diagnostics report is already JSON.

    ``date`` alone covers both cases: datetime subclasses it, and both spell
    isoformat().
    """
    if isinstance(value, date):
        return value.isoformat()
    return value


def _parse_daily_consumption(
    payload: Any, meter_id: str, start: datetime, end: datetime
) -> list[float | None] | None:
    """Pick one meter's daily figures out of a /consumer/consumption payload.

    The payload shape, read off the live endpoint:

        {"consumptionLines": [
            {"meter": {"meterId": 4711, ...},
             "consumptionValues": [
                 {"fromDate": "2026-01-01T00:00:00+01:00",
                  "toDate": "2026-01-02T00:00:00+01:00",
                  "consumption": 7},
                 ...]},
            ...]}

    Days are filtered by fromDate rather than trusted to match the request:
    the live endpoint has been seen to return one day more than was asked for.
    A day whose fromDate cannot be read is dropped for the same reason — it
    cannot be placed inside or outside the period.

    An unusable payload raises BrunataApiError, like every other parser here.
    A usable payload without the meter returns None: the call worked, it just
    has nothing to say about this meter.
    """
    if not isinstance(payload, dict):
        raise BrunataApiError(
            f"Expected a consumption object, got {type(payload).__name__}"
        )
    lines = payload.get("consumptionLines")
    if not isinstance(lines, list):
        raise BrunataApiError("Brunata consumption payload carried no consumptionLines")

    for line in lines:
        if not isinstance(line, dict):
            continue
        meter = line.get("meter")
        if not isinstance(meter, dict) or str(meter.get("meterId")) != meter_id:
            continue

        values = line.get("consumptionValues")
        if not isinstance(values, list):
            return None

        days: list[float | None] = []
        for value in values:
            if not isinstance(value, dict):
                continue
            from_date = parse_timestamp(value.get("fromDate"))
            if from_date is None or not start <= from_date < end:
                continue
            days.append(_parse_value(value.get("consumption")))
        return days

    return None


def _parse_meters(
    payload: Any,
    *,
    meter_types: list[str] | None = None,
    measurement_units: list[str] | None = None,
) -> ParsedMeters:
    """Turn a /consumer/metersforconsumer payload into meters keyed by ID.

    Each entry is flat — meter, latest reading and metadata side by side — so
    there is no wrapper object to guard against.

    Returns the meters *and* a ParseReport. The report exists because an
    absent meter is ambiguous: dismounted, unsupported, or merely skipped for
    a unit we could not name this poll. Only the first of those means the
    meter is gone, and the rest of the integration cannot tell without being
    told. See ParseReport.
    """
    if not isinstance(payload, list):
        raise BrunataApiError(
            f"Expected a list of meters, got {type(payload).__name__}"
        )

    _LOGGER.debug("Brunata returned %s raw item(s)", len(payload))

    meters: dict[str, BrunataMeter] = {}
    unresolved_units: set[str] = set()
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            _LOGGER.debug(
                "Item %s skipped: not an object (%s)", index, type(item).__name__
            )
            continue

        raw_id = item.get("meterId")
        if raw_id is None:
            _LOGGER.debug("Item %s skipped: meterId is null", index)
            continue

        # A dismounted meter is one Brunata has physically removed; its final
        # reading never changes again. Dropping it makes the entity go
        # unavailable, which is what a removed meter should look like.
        #
        # Checked before the allowlist. Both drop the item, so the order cannot
        # change which meters reach Home Assistant — but the allowlist writes a
        # log line asking the user to report the meter's type, and a meter
        # Brunata has taken off the wall is not one anybody needs identified.
        dismounted = parse_timestamp(item.get("dismountedDate"))
        if dismounted is not None:
            _LOGGER.debug(
                "Item %s (meter %s) skipped: dismounted on %s",
                index,
                raw_id,
                dismounted.date(),
            )
            continue

        # Before anything else is read: is this a kind of meter we are willing
        # to surface at all? See SUPPORTED_METER_TYPES.
        type_code = _meter_type_code(item.get("meterType"))
        if type_code not in SUPPORTED_METER_TYPES:
            _log_unsupported_meter(str(raw_id), repr(item.get("meterType")))
            continue

        # And is its unit something we can name? A code that does not resolve,
        # one Brunata left as "undefined", or none at all all say the same
        # thing: nobody knows what this meter measures in.
        #
        # Skipped rather than filled in. Home Assistant treats a changed unit
        # on an existing sensor as a different measurement and discards the
        # statistics behind it, permanently — so "8" in place of "m³" is a
        # permanent loss, while skipping is a pause the meter recovers from on
        # the next poll that resolves. Same rule as the allowlist above,
        # applied to the field where getting it wrong is the more expensive of
        # the two. The meter type keeps its fallback; see _lookup().
        unit = _resolve(
            measurement_units or [], item.get("unit"), "measurement unit"
        )
        if unit is None or unit.lower() == UNDEFINED_UNIT:
            _log_unresolved_unit(str(raw_id), repr(item.get("unit")))
            # Recorded, not just dropped. Brunata still reports this meter, so
            # its sensor must keep its value and stay available, and its
            # device must not become removable — see BrunataSensor.available
            # and async_remove_config_entry_device().
            unresolved_units.add(str(raw_id))
            continue

        value = _parse_value(item.get("latestReadingValue"))
        decimals = item.get("decimals")
        placement = item.get("placement")
        transmitting = item.get("transmitting")
        allocation_unit = item.get("allocationUnit")

        _LOGGER.debug(
            "Item %s: meterId=%r meterType=%r unit=%r decimals=%r "
            "transmitting=%r has reading=%s",
            index,
            raw_id,
            item.get("meterType"),
            item.get("unit"),
            decimals,
            transmitting,
            value is not None,
        )

        meters[str(raw_id)] = BrunataMeter(
            meter_id=str(raw_id),
            meter_no=item.get("meterNo"),
            meter_type=_lookup(meter_types or [], item.get("meterType"), "meter type"),
            # The code the allowlist check above was made on, not a second
            # reading of the field: the two must never be able to disagree.
            meter_type_code=type_code,
            unit=unit,
            value=value,
            reading_date=parse_reading_date(item.get("latestReadingDate")),
            reading_time=_aware_or_none(
                parse_timestamp(item.get("latestReadingDate"))
            ),
            placement=placement if isinstance(placement, str) and placement else None,
            mounting_date=parse_timestamp(item.get("mountingDate")),
            # Three things have to hold before this becomes a display
            # precision. It must be an int, because Home Assistant counts
            # digits with it. It must not be a bool, because bool subclasses
            # int and `true` would otherwise become a precision of 1 —
            # _meter_type_code() guards the same way, and the two fields read
            # the same kind of input. And it must not be negative: there is no
            # such thing as minus four decimal places, and passing one on would
            # put a number Brunata never reported into the entity.
            #
            # Anything else is dropped, and sensor.py falls back to a precision
            # chosen from the unit.
            decimals=(
                int(decimals)
                if isinstance(decimals, int)
                and not isinstance(decimals, bool)
                and decimals >= 0
                else None
            ),
            transmitting=transmitting if isinstance(transmitting, bool) else None,
            allocation_unit=(
                allocation_unit.strip()
                if isinstance(allocation_unit, str) and allocation_unit.strip()
                else None
            ),
        )

    _LOGGER.debug("Parsed %s meters from Brunata", len(meters))
    return ParsedMeters(
        meters, ParseReport(frozenset(unresolved_units), len(payload))
    )

"""Tests for the API layer's response handling.

These paths decide whether a user sees stale-but-working sensors, a temporary
failure, or a re-authentication prompt. They previously lived inside the
coordinator alongside the fetch logic and had no coverage at all, which is how
the 429 back-off ended up being dead code for months: it looked correct in
review and nothing exercised it.
"""

from datetime import date

import logging
import re
import time

import httpx
import pytest

from custom_components.brunata.api import (
    API_URL,
    DEFAULT_HEADERS,
    REFERER_URL,
    SUPPORTED_METER_TYPES,
    BrunataApiClient,
    BrunataApiError,
    BrunataAuthError,
    BrunataConnectionError,
    _parse_meters,
    _payload,
)


class FakeResponse:
    def __init__(self, status_code=200, *, json_data=None, raises=None, headers=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._json_data = json_data
        self._raises = raises

    def json(self):
        if self._raises is not None:
            raise self._raises
        return self._json_data


# Brunata's two lookup tables, transcribed in full from a debug log of a live
# account on 3 September 2026. The payload carries indices into these; the
# names and units the user ends up seeing come from here.
#
# They are reproduced whole, and with their oddities intact, because a fixture
# that says "this is what Brunata sends" gets read as a reading months later.
# An earlier version had eight invented entries with "Energy" at index 3. That
# was a guess, and it was wrong — energy is 6.
#
# Reserved slots are None, exactly as Brunata sends them: 7 of the 28 meter
# types and 34 of the 96 units are unused.
METER_TYPES = [
    "Collector",             # 0
    "Radiator",              # 1  heat cost allocator
    "Water",                 # 2
    "Temperature",           # 3
    "Gas",                   # 4
    "Electricity ",          # 5  trailing space is Brunata's, not a typo
    "Energy",                # 6
    "Humidity",              # 7
    "Fictive",               # 8
    "Hour counter",          # 9
    "Oxygen",                # 10
    "Meter visualization",   # 11
    "Smoke detector",        # 12
    "Leakage detector",      # 13
    "Climate sensor",        # 14
    "Carbon dioxide ",       # 15 trailing space is Brunata's
    "Acceleration Sensor",   # 16
    "Vibration Sensor",      # 17
    "Pressure sensor",       # 18
    "Smart Sensors",         # 19
    None,                    # 20
    None,                    # 21
    None,                    # 22
    None,                    # 23
    None,                    # 24
    None,                    # 25
    None,                    # 26
    "RME95",                 # 27
]

# Written as index -> name so the 34 reserved slots do not have to be counted
# out by hand. Index 8 is m³, which is what live water meters report, and
# index 1 is units, which is what heat cost allocators report. kWh and units
# each appear twice (7/16 and 1/12); that is Brunata's table, not a mistake
# here, and it makes no difference because lookups go by index.
_MEASUREMENT_UNIT_NAMES = {
    0: "undefined", 1: "units", 2: "Wh", 3: "MWh", 4: "GJ", 5: "GCal",
    6: "Btu", 7: "kWh", 8: "m³", 9: "liter", 10: "°C", 11: "hours",
    12: "units", 13: "m³ per hour", 14: "RH %", 15: "J", 16: "kWh",
    17: "day", 18: "Dal", 19: "MJ", 20: "kJ",
    # 21-50 reserved
    51: "EM units", 52: "RME82 units", 53: "RMK units",
    # 54-55 reserved
    56: "CLK units", 57: "VVM units", 58: "Clorius units",
    # 59-60 reserved
    61: "Doprimo units", 62: "RME80 units", 63: "Clorius C9 units",
    64: "K&L units", 65: "Kundo units", 66: "L&S units", 67: "Zenner units",
    68: "Minometer units", 69: "VVME80 units", 70: "VVM88 units",
    71: "VVME87 units", 72: "Geysir units", 73: "W", 74: "dismounts",
    75: "functionality test", 76: "Kcal", 77: "Mcal", 78: "state", 79: "ppm",
    80: "m³/s", 81: "m³/min", 82: "lx", 83: "counts", 84: "%", 85: "hPa",
    86: "level", 87: "Hz", 88: "g", 89: "m/s²", 90: "m²", 91: "‰",
    92: "shares", 93: "kPa", 94: "m", 95: "µS/cm",
}
MEASUREMENT_UNITS = [_MEASUREMENT_UNIT_NAMES.get(i) for i in range(96)]


def _parse(payload):
    """The meters half of a parse. _report() below is the other half."""
    return _parse_meters(
        payload, meter_types=METER_TYPES, measurement_units=MEASUREMENT_UNITS
    ).meters


def _report(payload):
    """The ParseReport half — what the parse saw beyond the meters."""
    return _parse_meters(
        payload, meter_types=METER_TYPES, measurement_units=MEASUREMENT_UNITS
    ).report


def _meter_item(meter_id="12345", *, dismounted=None, reading=True, value=42.0):
    """One entry shaped like the real /consumer/metersforconsumer payload."""
    return {
        "meterId": int(meter_id) if str(meter_id).isdigit() else meter_id,
        "placement": "Koldt vand",
        "meterNo": f"M{meter_id}",
        "meterType": 2,
        "mountingDate": "2018-10-23T14:10:40+02:00",
        "dismountedDate": dismounted,
        "allocationUnit": "K",
        "unit": "8",
        "printedSerialNo": None,
        "decimals": 3,
        "latestReadingDate": "2026-08-23T08:53:00+02:00" if reading else None,
        "latestReadingValue": value if reading else None,
        "transmitting": True,
    }


@pytest.mark.parametrize(
    ("status", "match"),
    [(429, "rate limit"), (503, "server error"), (404, "not found")],
)
def test_error_statuses_raise_api_error(status, match):
    """None of these are auth problems, so none of them may prompt the user for
    credentials."""
    with pytest.raises(BrunataApiError, match=match) as err:
        _payload(FakeResponse(status))
    assert err.value.status == status


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"Retry-After": "120"}, 120.0),
        ({"Retry-After": " 90 "}, 90.0),
        ({}, None),
        # The HTTP-date form is deliberately not read: it needs Brunata's clock
        # to agree with ours, and guessing wrong means waiting far too long or
        # not at all. The caller falls back to a fixed hour instead.
        ({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, None),
        ({"Retry-After": "0"}, None),
        ({"Retry-After": "-5"}, None),
        # float() accepts all three of these, and inf is greater than zero, so
        # they used to pass straight through to timedelta(seconds=...) in the
        # coordinator — which raises OverflowError from inside the very except
        # block that exists to translate this error. See the docstring on
        # _retry_after_seconds().
        ({"Retry-After": "inf"}, None),
        ({"Retry-After": "Infinity"}, None),
        ({"Retry-After": "1e400"}, None),
        ({"Retry-After": "nan"}, None),
        # Enormous but real. The parser passes it on; the coordinator caps it.
        ({"Retry-After": "1e12"}, 1e12),
    ],
)
def test_retry_after_is_read_from_a_rate_limit(headers, expected):
    """429 is the one status where retrying quickly is actively harmful, so
    what Brunata asks for has to survive the trip to the coordinator."""
    with pytest.raises(BrunataApiError) as err:
        _payload(FakeResponse(429, headers=headers))
    assert err.value.status == 429
    assert err.value.retry_after == expected


def test_only_a_rate_limit_carries_a_retry_after():
    """A 503 is Brunata having a bad day, not a request to stay away."""
    with pytest.raises(BrunataApiError) as err:
        _payload(FakeResponse(503, headers={"Retry-After": "3600"}))
    assert err.value.retry_after is None


def test_unparseable_json_raises_api_error():
    with pytest.raises(BrunataApiError, match="invalid JSON"):
        _payload(FakeResponse(200, raises=ValueError("nope")))


def test_auth_error_in_body_is_an_auth_error():
    """Brunata reports some auth failures with HTTP 200 and an error body
    instead of a proper 401."""
    with pytest.raises(BrunataAuthError):
        _payload(
            FakeResponse(
                200,
                json_data={
                    "errorCode": "WB_WEBSERVICES_0011",
                    "errorMessage": "Not authorized",
                },
            )
        )


def test_other_error_in_body_is_not_an_auth_error():
    """A non-auth error body must not send the user off re-entering a password
    that was never wrong."""
    with pytest.raises(BrunataApiError, match="WB_SOMETHING_ELSE"):
        _payload(
            FakeResponse(
                200,
                json_data={"errorCode": "WB_SOMETHING_ELSE", "errorMessage": "Boom"},
            )
        )


def test_successful_payload_is_returned_untouched():
    payload = [_meter_item()]
    assert _payload(FakeResponse(200, json_data=payload)) is payload


def test_parses_meter_and_reading():
    meters = _parse([_meter_item("abc", value=13.5)])

    assert set(meters) == {"abc"}
    meter = meters["abc"]
    assert meter.meter_id == "abc"
    assert meter.meter_no == "Mabc"
    assert meter.meter_type == "Water"
    assert meter.unit == "m³"
    assert meter.value == 13.5
    assert meter.reading_date == date(2026, 8, 23)


def test_meter_without_a_reading_has_no_value():
    """A meter can exist before it has reported. It must still appear, so the
    entity is created and can pick up a value later."""
    meters = _parse([_meter_item("abc", reading=False)])

    assert meters["abc"].value is None
    assert meters["abc"].reading_date is None


def test_dismounted_meters_are_skipped():
    """A meter Brunata has physically removed never reports again. Carrying it
    would leave a device in Home Assistant frozen on its final reading,
    indistinguishable from a working one."""
    meters = _parse(
        [
            _meter_item("111"),
            _meter_item("222", dismounted="2026-03-01T09:00:00+01:00"),
        ]
    )
    assert set(meters) == {"111"}


def test_meter_without_an_id_is_skipped():
    """Previously str(None) produced a meter keyed "None", and an entity with
    unique_id brunata_None_consumption."""
    item = _meter_item("x")
    item["meterId"] = None

    assert _parse([item]) == {}


def test_malformed_entries_are_skipped_not_fatal():
    """One bad row must not cost the user every other meter."""
    meters = _parse(
        ["not a dict", {}, _meter_item("good")]
    )
    assert set(meters) == {"good"}


@pytest.mark.parametrize("raw_value", ["12,5", "n/a", "", {}, []])
def test_unparseable_reading_value_costs_one_meter_not_all_of_them(raw_value):
    """A bare float() would raise straight out of _parse_meters(), past every
    error type api.py defines, and reach DataUpdateCoordinator as an unexpected
    exception — so one bad row would cost every meter that update. Every other
    field in the payload already degrades to None; this one has to as well."""
    item = _meter_item("abc")
    item["latestReadingValue"] = raw_value

    meters = _parse([item, _meter_item("good")])

    assert set(meters) == {"abc", "good"}
    assert meters["abc"].value is None
    assert meters["good"].value == 42.0


@pytest.mark.parametrize(
    "raw_value", ["NaN", "nan", "Infinity", "inf", "-inf", "1e400", float("nan")]
)
def test_a_non_finite_reading_value_is_dropped(raw_value):
    """float() accepts every one of these, and none of them is a reading.

    nan is the one that matters. Every comparison against it is false, so
    `previous is None or value >= previous` takes it whenever there is no
    previous value — the first poll after setup, or after a restart with
    nothing to restore — and nan becomes the sensor's state. With a previous
    value it goes the other way and is logged as a decrease that is neither a
    replacement nor an annual reset, which is a warning about something that
    never happened.

    The other meter in the payload must survive either way, same as every
    other bad-row test here.
    """
    item = _meter_item("abc")
    item["latestReadingValue"] = raw_value

    meters = _parse([item, _meter_item("good")])

    assert set(meters) == {"abc", "good"}
    assert meters["abc"].value is None
    assert meters["good"].value == 42.0


def test_reading_value_as_a_numeric_string_is_accepted():
    """The unit field already arrives as a string in this payload where it was
    an integer in the old one, so a value doing the same is not far-fetched."""
    item = _meter_item("abc")
    item["latestReadingValue"] = "151.037"

    assert _parse([item])["abc"].value == 151.037


def test_unparseable_reading_date_leaves_the_value_intact():
    item = _meter_item("abc")
    item["latestReadingDate"] = "not-a-date"

    meter = _parse([item])["abc"]
    assert meter.value == 42.0
    assert meter.reading_date is None


def test_timestamp_reading_date_is_accepted():
    item = _meter_item("abc")
    item["latestReadingDate"] = "2026-01-01T04:00:00Z"

    assert _parse([item])["abc"].reading_date == date(2026, 1, 1)


@pytest.mark.parametrize(
    ("type_code", "unit_code", "expected_type", "expected_unit"),
    [(1, 1, "Radiator", "units"), (2, 8, "Water", "m³")],
)
def test_codes_are_resolved_through_the_lookup_tables(
    type_code, unit_code, expected_type, expected_unit
):
    """The payload carries indices, not names. Resolving them wrong is not
    cosmetic: the device is named after the type, and Home Assistant suppresses
    a sensor's long term statistics when its unit changes, so water meters
    silently stopped recording."""
    item = _meter_item("abc")
    item["meterType"] = type_code
    item["unit"] = unit_code

    meter = _parse([item])["abc"]
    assert meter.meter_type == expected_type
    assert meter.unit == expected_unit


def test_a_null_meter_type_entry_falls_back_to_the_raw_code():
    """Brunata's live tables contain null entries — 7 of 28 meter types and 34
    of 96 units are reserved slots. Returning the None would put it straight
    into BrunataMeter.meter_type, and the sensor platform would die on
    meter_type.lower(), taking every entity with it.

    The meter type falls back to the raw code, because that name only reaches
    the device name and the model field: a device called "1" is ugly, visible,
    and fixes itself the moment the table resolves again. The unit does not —
    see the test below.

    The unit code is set to one the table can answer. Left at the payload's
    own "8", it lands on a reserved null slot in the unit table below, the
    meter is skipped, and this test would be asserting the unit rule while
    claiming to assert the type rule."""
    types = ["Collector", None, "Water"] + [None] * 25
    units = ["undefined", "units"] + [None] * 94

    item = _meter_item("abc")
    item["meterType"] = 1
    item["unit"] = 1

    meter = _parse_meters(
        [item], meter_types=types, measurement_units=units
    ).meters["abc"]
    assert meter.meter_type == "1"
    assert meter.meter_type_code == 1
    assert meter.unit == "units"


@pytest.mark.parametrize("code", [21, 99, -1, "-1", -99, 8.0, "eight", None, ""])
def test_a_meter_whose_unit_cannot_be_resolved_is_skipped(code):
    """The unit gets no fallback, and that asymmetry is the whole point.

    Home Assistant treats a changed unit on an existing sensor as a different
    measurement and discards the long term statistics behind the old one. So a
    meter carrying "99" where the user had "m³" does not lose a label — it
    loses years of history, permanently. Skipping costs a pause the meter
    recovers from by itself on the next poll that resolves.

    Every value here is a way the table can fail to answer: past the end, a
    reserved null slot, a negative index Python would otherwise read from the
    wrong end of the list, a float, a non-number, and nothing at all.

    If this test ever needs relaxing, that is the moment to think very hard
    about what is being traded for what.
    """
    units = ["undefined", "units"] + [None] * 94

    item = _meter_item("abc")
    item["unit"] = code

    assert _parse_meters(
        [item], meter_types=METER_TYPES, measurement_units=units
    ).meters == {}


def test_a_meter_brunata_calls_undefined_is_skipped():
    """Index 0 of the unit table is the literal string "undefined".

    It resolves, so it is not caught by the test above — but it is Brunata
    saying it has not stated a unit, which is the same position as a code that
    does not resolve, and it gets the same answer. It used to become "units",
    which is a claim nobody had read anywhere."""
    item = _meter_item("abc")
    item["unit"] = 0

    assert _parse_meters(
        [item], meter_types=METER_TYPES, measurement_units=["undefined", "units"]
    ).meters == {}


def test_one_unusable_unit_does_not_cost_the_other_meters():
    """The skip drops one item, not the response — the same shape as the
    meterType allowlist."""
    good = _meter_item("good")
    bad = _meter_item("bad")
    bad["unit"] = 99

    assert sorted(_parse([bad, good])) == ["good"]


@pytest.mark.parametrize("code", [1, 2, 5])
def test_supported_meter_types_are_parsed(code):
    """The three codes on the allowlist: 1 = heat cost allocator (radiator),
    2 = water, 5 = electricity. All three are read off Brunata's own meterType
    table rather than guessed, and all three have produced a working entity —
    see SUPPORTED_METER_TYPES."""
    item = _meter_item("abc")
    item["meterType"] = code

    assert "abc" in _parse([item])


@pytest.mark.parametrize("code", [1, 2, 5])
def test_the_numeric_meter_type_is_carried_alongside_its_name(code):
    """sensor.py decides the 1 January reset from this number.

    The name next to it is a translation from Brunata's locale table, so a
    rule matching on it would switch itself off the day the entry was
    relabelled. Carried from the same value the allowlist check above was made
    on, so the two cannot disagree.
    """
    item = _meter_item("abc")
    item["meterType"] = code

    assert _parse([item])["abc"].meter_type_code == code


def test_the_numeric_meter_type_survives_an_unresolvable_name():
    """The code is read off the payload, not out of the lookup table, so it is
    still there when the name falls back to the raw code.

    The unit table is supplied, because an unresolvable unit would now skip the
    meter before this could be asserted — and this test is about the type."""
    item = _meter_item("abc")
    item["meterType"] = 1

    meter = _parse_meters(
        [item], meter_types=[], measurement_units=MEASUREMENT_UNITS
    ).meters["abc"]

    assert meter.meter_type == "1"
    assert meter.meter_type_code == 1


def test_a_meter_type_given_as_a_string_is_carried_as_an_int():
    """`unit` arrives as a string in this payload, so meterType could too. The
    code must be comparable to the ints in SUPPORTED_METER_TYPES and
    ANNUAL_RESET_METER_TYPES either way."""
    item = _meter_item("abc")
    item["meterType"] = "1"

    assert _parse([item])["abc"].meter_type_code == 1


def test_detectors_never_become_entities():
    """The whole reason SUPPORTED_METER_TYPES exists.

    Brunata's table carries a smoke detector at 12 and a leakage detector at
    13. This integration polls hourly over a cloud API, so an entity for either
    would invite an automation that could be an hour late. If this test is ever
    softened, that is the moment to think very hard: no amount of usefulness
    elsewhere buys back a fire alarm that fires at the top of the next hour.
    """
    assert 12 not in SUPPORTED_METER_TYPES
    assert 13 not in SUPPORTED_METER_TYPES


def test_the_allowlist_is_what_the_table_says_it_is():
    """The numbers, written down once, next to what they mean.

    Read off Brunata's own meterType table via a debug log from a live
    account. Indices 1 and 2 in that table match the meters that can be
    verified against real entities, which is what makes 5 a reading rather
    than the guess issue #39 refused to make.

    Index 6 is asserted too, and is deliberately absent from the allowlist.
    Knowing what a code means and choosing to surface it are separate
    decisions, and this test holds both: the reading is recorded so nobody has
    to find it again, and energy stays out until somebody has such a meter.
    """
    assert SUPPORTED_METER_TYPES == frozenset({1, 2, 5})
    assert METER_TYPES[1] == "Radiator"
    assert METER_TYPES[2] == "Water"
    # Brunata's own entry has a trailing space. The fixture keeps it so the
    # stripping below is exercised against the real string rather than an
    # invented one.
    assert METER_TYPES[5] == "Electricity "
    assert METER_TYPES[6] == "Energy"
    assert 6 not in SUPPORTED_METER_TYPES

    electricity = _meter_item("abc")
    electricity["meterType"] = 5
    electricity["unit"] = 7
    meter = _parse([electricity])["abc"]
    assert meter.meter_type == "Electricity"
    assert meter.unit == "kWh"


# 12 and 13 are the smoke and leakage detectors, named explicitly because
# they are the reason this boundary exists at all. 6 is energy: the table
# names it, but it stays off the allowlist until somebody actually has one
# — see SUPPORTED_METER_TYPES. 4 is gas and 3 is temperature: real types
# Brunata can report that this integration has not been built for. 27 is
# the last named entry in the table, -1 and 99 are outside it.
@pytest.mark.parametrize("code", [0, 3, 4, 6, 9, 12, 13, 17, 27, -1, 99])
def test_unsupported_meter_types_never_become_entities(code):
    """The safety boundary. Brunata's portal can carry leak and smoke
    detectors, and this integration polls once an hour over a cloud API — up
    to 59 minutes and 30 seconds can pass between an event and Home Assistant
    hearing about it.

    An entity invites an automation, and that automation would be dangerously
    slow. Nothing downstream can undo it once the entity exists, so the meter
    has to be dropped here. If this test ever needs relaxing, that is the
    moment to think very hard about why.

    Everything in this list is a code the table names or leaves empty, and
    none of it is a guess: 3 is temperature, 4 is gas, 6 is energy, 12 and 13
    are the detectors, and 20-26 are unfilled slots. Knowing what a code means
    and choosing to surface it are separate decisions — 6 is the clearest
    case, and it stays out until somebody actually has such a meter."""
    item = _meter_item("abc")
    item["meterType"] = code

    assert _parse([item]) == {}


@pytest.mark.parametrize(
    "raw", [None, "", "abc", "1.5", {}, [], True, False, float("nan")]
)
def test_unusable_meter_type_fails_closed(raw):
    """A meterType that cannot be checked against the allowlist is treated as
    unsupported, not as supported. Failing open here would put exactly the
    meters we cannot identify into Home Assistant."""
    item = _meter_item("abc")
    item["meterType"] = raw

    assert _parse([item]) == {}


@pytest.mark.parametrize("raw", [{}, [], {"a": 1}, [1, 2]])
def test_unhashable_meter_type_does_not_take_down_the_parse(raw):
    """The skip is logged through an lru_cache, which hashes its arguments.
    Passing the raw meterType in raised TypeError out of _parse_meters() for
    a dict or a list — costing every meter that update, which is the exact
    failure the filter exists to prevent. The caller formats it first."""
    good = _meter_item("good")
    bad = _meter_item("bad")
    bad["meterType"] = raw

    assert sorted(_parse([bad, good])) == ["good"]


def test_meter_type_as_a_numeric_string_is_accepted():
    """`unit` arrives as a string in this payload where it was an integer in
    the old one, so meterType could do the same."""
    item = _meter_item("abc")
    item["meterType"] = "2"

    assert "abc" in _parse([item])


def test_a_supported_meter_survives_alongside_an_unsupported_one():
    """The filter drops one item, not the whole response."""
    water = _meter_item("water")
    water["meterType"] = 2
    detector = _meter_item("detector")
    detector["meterType"] = 13

    assert sorted(_parse([water, detector])) == ["water"]


def test_trailing_whitespace_in_table_entries_is_stripped():
    """The live table has entries with a trailing space; left unstripped it
    would end up in the device name verbatim."""
    item = _meter_item("abc")
    item["meterType"] = 1

    meter = _parse_meters(
        [item], meter_types=["", "Radiator "], measurement_units=MEASUREMENT_UNITS
    ).meters["abc"]
    assert meter.meter_type == "Radiator"


def test_a_negative_unit_code_does_not_wrap_round_the_table():
    """Python indexes lists from both ends. Brunata does not.

    `table[-1]` does not raise IndexError — it returns the *last* entry, so a
    negative unit code resolved to a real, wrong unit: wrong device class,
    wrong state class, and long term statistics that look right and are not.
    It happened silently, because the warning only fires when the lookup fails.

    The guard is still what this covers; what changed is the outcome. The meter
    is skipped rather than given the raw code, so the assertion is that
    "THE-LAST-ENTRY" is nowhere near the result.
    """
    item = _meter_item("abc")
    item["unit"] = -1

    assert _parse_meters(
        [item],
        meter_types=METER_TYPES,
        measurement_units=["undefined", "units", "m3", "THE-LAST-ENTRY"],
    ).meters == {}


def test_an_unknown_meter_type_code_still_falls_back():
    """The meter type keeps its fallback, and this is the test that says so.

    Passing a code through as-is once crashed every entity on
    meter.meter_type.lower(), not just the unrecognised one. The name reaches
    the device name and the model field and nothing else — every decision hangs
    on meter_type_code — so a device called "2" is a cosmetic problem that
    fixes itself when the table resolves again.

    A short meter type table is used so that meterType 2 — supported, so it
    reaches the lookup — lands past the end of it."""
    item = _meter_item("abc")
    item["meterType"] = 2

    meter = _parse_meters(
        [item], meter_types=["Collector", "Radiator"],
        measurement_units=MEASUREMENT_UNITS,
    ).meters["abc"]
    assert meter.meter_type == "2"
    assert meter.meter_type_code == 2
    # And the unit, which did resolve, is untouched by the type's trouble.
    assert meter.unit == "m³"


def test_without_lookup_tables_no_meter_is_created():
    """The locale resource failing to load is already an error the coordinator
    turns into a retry, so this path should not be reachable in production —
    but if it ever is, no meter may come out of it.

    Every unit is unresolvable without the table, so every meter is skipped.
    That is the safe end: the sensors keep the values and the history they
    already have, and the next poll that loads the tables brings them back."""
    assert _parse_meters([_meter_item("abc")]).meters == {}


def test_a_skipped_meter_type_is_logged_once_per_run(caplog):
    """A skipped meter must not fill the log with the same line every hour.

    The integration polls once an hour, so a meter that stays unsupported
    would write 24 identical lines a day forever. The log line is therefore
    cached per meter and per code, and fires once for the lifetime of the
    Home Assistant process — long enough to be found in a bug report, short of
    being noise.

    Nothing held that in place before: removing the cache left every test
    green. The meter id here is unique to this test so the cache is guaranteed
    to be cold, whatever ran before it.
    """
    item = _meter_item("logged-once-type")
    item["meterType"] = 13

    with caplog.at_level(logging.INFO, logger="custom_components.brunata.api"):
        _parse([item])
        _parse([item])
        _parse([item])

    lines = [r for r in caplog.records if "logged-once-type" in r.getMessage()]
    assert len(lines) == 1


def test_a_skipped_unit_is_logged_once_per_run(caplog):
    """The same rule for the other skip. See the test above."""
    item = _meter_item("logged-once-unit")
    item["unit"] = 99

    with caplog.at_level(logging.WARNING, logger="custom_components.brunata.api"):
        _parse([item])
        _parse([item])
        _parse([item])

    lines = [r for r in caplog.records if "logged-once-unit" in r.getMessage()]
    assert len(lines) == 1


def test_a_skipped_unit_is_reported_rather_than_silently_dropped():
    """The meter is skipped, but it is not gone, and the difference matters.

    Absence from the meters dictionary is ambiguous: dismounted, unsupported,
    or — as here — still on the wall with a unit we could not name this poll.
    Only the first means the meter is gone. Without the report, the sensor
    would go unavailable and the device would become removable, both wrong for
    a meter Brunata is still listing.
    """
    units = ["undefined", "units"] + [None] * 94

    bad = _meter_item("bad")
    bad["unit"] = 99
    good = _meter_item("good")
    # Index 1, not the item default of "8": index 8 is one of the reserved
    # null slots in the table above, so leaving it would skip this meter too
    # and the test would assert nothing.
    good["unit"] = 1

    parsed = _parse_meters(
        [bad, good], meter_types=METER_TYPES, measurement_units=units
    )

    assert set(parsed.meters) == {"good"}
    assert parsed.report.unresolved_unit_meter_ids == frozenset({"bad"})


def test_an_unsupported_meter_type_is_not_reported_as_unresolved():
    """The two skips are not the same thing.

    An unsupported meterType never becomes an entity in the first place, so
    there is nothing to keep available and nothing to protect from deletion.
    Only the unit skip concerns a meter that already has one.
    """
    detector = _meter_item("detector")
    detector["meterType"] = 13

    assert _report([detector]).unresolved_unit_meter_ids == frozenset()


def test_a_dismounted_meter_is_not_reported_as_unresolved():
    """A dismounted meter really is gone, and must stay removable."""
    item = _meter_item("gone", dismounted="2026-03-01T09:00:00+01:00")

    assert _report([item]).unresolved_unit_meter_ids == frozenset()


@pytest.mark.parametrize(("payload", "expected"), [([], 0), ([_meter_item("a")], 1)])
def test_the_raw_item_count_is_what_brunata_sent(payload, expected):
    """Counted before any filtering.

    Zero is the value that matters: it means Brunata reported nothing at all,
    which is a different fault from "Brunata says you have no meters" and is
    not evidence that any particular meter is gone.
    """
    assert _report(payload).raw_item_count == expected


def test_an_empty_payload_parses_to_a_successful_update_with_no_meters():
    """The premise behind async_remove_config_entry_device()'s raw count check.

    Nothing here raises, so the coordinator records a successful update with
    an empty data dictionary. last_update_success is therefore not enough to
    tell "every meter is gone" from "Brunata told us nothing".
    """
    parsed = _parse_meters([], meter_types=METER_TYPES, measurement_units=[""])

    assert parsed.meters == {}
    assert parsed.report.raw_item_count == 0


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("too slow"),
        httpx.RemoteProtocolError("garbled"),
        # The two RequestError adds over TransportError. Caught as
        # TransportError only, these escaped api.py untranslated and reached
        # the coordinator as unexpected exceptions with a traceback instead of
        # as a clean retry.
        httpx.TooManyRedirects("redirect loop"),
        httpx.DecodingError("bad content-encoding"),
    ],
)
async def test_request_failures_become_a_connection_error(error):
    """Everything under httpx.RequestError means the same thing here.

    The request produced no usable response, so the coordinator should keep
    the last known values and try again next hour.
    """

    class FakeHttp:
        async def request(self, method, url, **kwargs):
            raise error

    client = BrunataApiClient("user@example.com", "s3cret", FakeHttp())

    with pytest.raises(BrunataConnectionError):
        await client._async_request("GET", "https://example.invalid")


async def test_a_url_we_built_wrong_is_not_disguised_as_a_network_problem():
    """httpx.InvalidURL sits outside RequestError, and that is why the handler
    is RequestError rather than HTTPError.

    A malformed URL is this integration's own mistake. Translating it into
    BrunataConnectionError would turn it into an hourly "cannot reach Brunata"
    that no amount of waiting fixes.
    """

    class FakeHttp:
        async def request(self, method, url, **kwargs):
            raise httpx.InvalidURL("not a url")

    client = BrunataApiClient("user@example.com", "s3cret", FakeHttp())

    with pytest.raises(httpx.InvalidURL):
        await client._async_request("GET", "not a url")


async def test_the_browser_headers_reach_the_http_client(hass):
    """The headers are the whole defence against Brunata's bot protection.

    They are set in one place — when async_create() builds the HTTP client —
    and every other test in this file goes around it with a fake client. Delete
    the headers and the entire suite stays green, while logins start failing
    for users with no explanation in the log.

    This is the one test that builds the real client.
    """
    client = await BrunataApiClient.async_create(hass, "user@example.com", "s3cret")
    try:
        for name, value in DEFAULT_HEADERS.items():
            assert client._client.headers[name] == value
    finally:
        await client.async_close()


def test_the_browser_version_is_the_same_in_all_four_places():
    """Bumping the Edge version means editing four strings, and missing one
    produces headers no real browser would send — which is worse than being a
    version behind, because it is exactly the inconsistency bot protection
    looks for.

    Nothing can tell whether the version is current; that stays a manual job.
    This only holds the four to each other.
    """
    user_agent = DEFAULT_HEADERS["User-Agent"]
    client_hints = DEFAULT_HEADERS["Sec-Ch-Ua"]

    versions = {
        re.search(r"Chrome/(\d+)", user_agent).group(1),
        re.search(r"Edg/(\d+)", user_agent).group(1),
        re.search(r'"Chromium";v="(\d+)"', client_hints).group(1),
        re.search(r'"Microsoft Edge";v="(\d+)"', client_hints).group(1),
    }

    assert len(versions) == 1, f"Edge version differs between the headers: {versions}"


async def test_the_lookup_tables_are_loaded_before_the_meters_are_parsed():
    """Order, not just outcome.

    async_get_meters() fetches the locale resource first and then parses the
    meter payload against it. Reversed, the tables would be empty at parse
    time, every unit would fail to resolve, every meter would be skipped — and
    the update would *succeed* with nothing in it. Nothing else in the suite
    notices, because the other endpoint test presets the tables itself.
    """
    calls: list[str] = []

    class FakeHttp:
        async def request(self, method, url, **kwargs):
            calls.append(url)
            if url.endswith("/locales/en/common"):
                return FakeResponse(
                    200,
                    json_data={
                        "mappers": {
                            "meterType": METER_TYPES,
                            "measurementUnit": MEASUREMENT_UNITS,
                        }
                    },
                )
            return FakeResponse(200, json_data=[_meter_item("abc")])

    client = BrunataApiClient("user@example.com", "s3cret", FakeHttp())
    client._access_token = "T"
    client._expires_at = time.time() + 300

    meters = await client.async_get_meters()

    assert calls[0].endswith("/locales/en/common")
    assert calls[1].endswith("/consumer/metersforconsumer")
    assert set(meters) == {"abc"}


async def test_the_parse_report_describes_the_call_it_came_from():
    """The coordinator reads the report immediately after the meters, and the
    two have to describe the same poll."""

    class FakeHttp:
        async def request(self, method, url, **kwargs):
            return FakeResponse(200, json_data=[_meter_item("abc")])

    client = BrunataApiClient("user@example.com", "s3cret", FakeHttp())
    client._access_token = "T"
    client._expires_at = time.time() + 300
    client._meter_types = METER_TYPES
    client._measurement_units = MEASUREMENT_UNITS
    client._lookup_tables_loaded = True

    # Before any call: nothing has been reported, so nothing may be concluded.
    assert client.last_parse_report().raw_item_count == 0

    await client.async_get_meters()

    assert client.last_parse_report().raw_item_count == 1
    assert client.last_parse_report().unresolved_unit_meter_ids == frozenset()


def test_non_list_payload_raises_api_error():
    with pytest.raises(BrunataApiError, match="Expected a list"):
        _parse({"unexpected": True})


# --- fields that only /consumer/metersforconsumer carries -----------------


def test_placement_is_read_from_the_payload():
    """The label the customer set in Brunata's own UI. It becomes the device
    name, so "Koldt vand" beats "Water (7822808)"."""
    assert _parse([_meter_item("abc")])["abc"].placement == "Koldt vand"


@pytest.mark.parametrize("placement", [None, "", 42])
def test_unusable_placement_becomes_none(placement):
    item = _meter_item("abc")
    item["placement"] = placement

    assert _parse([item])["abc"].placement is None


def test_mounting_date_is_parsed_with_its_offset():
    """The replacement signal. It must survive as a comparable value, or a
    meter swap looks identical to a glitch."""
    meter = _parse([_meter_item("abc")])["abc"]

    assert meter.mounting_date is not None
    assert meter.mounting_date.year == 2018
    assert meter.mounting_date.utcoffset() is not None


def test_unparseable_mounting_date_is_not_fatal():
    item = _meter_item("abc")
    item["mountingDate"] = "whenever"

    assert _parse([item])["abc"].mounting_date is None


def test_decimals_and_transmitting_are_carried_through():
    """decimals drives the displayed precision; Brunata states it per meter
    rather than leaving it to be guessed from the unit."""
    meter = _parse([_meter_item("abc")])["abc"]

    assert meter.decimals == 3
    assert meter.transmitting is True


@pytest.mark.parametrize("decimals", [None, "3", 1.5, True, False])
def test_non_integer_decimals_is_ignored(decimals):
    """True and False are in this list because bool subclasses int.

    `isinstance(True, int)` is True, so a payload carrying `true` used to
    become a display precision of 1 — a number the meter never reported.
    """
    item = _meter_item("abc")
    item["decimals"] = decimals

    assert _parse([item])["abc"].decimals is None


def test_a_negative_decimals_is_ignored():
    """There is no such thing as minus four decimal places.

    decimals sets how many digits Home Assistant shows. The guard beside it
    already rejects bools and non-integers; a negative number is the same kind
    of unusable input and was the one case that slipped through. Passing it on
    would show the reading rounded to a number Brunata never reported.

    Not seen in any payload. The point is that the field is checked the same
    way in every direction.
    """
    item = _meter_item("abc")
    item["decimals"] = -4

    assert _parse([item])["abc"].decimals is None


async def test_meters_are_fetched_from_metersforconsumer():
    """The endpoint itself. /consumer/meters answers too, with a different
    shape and without placement, mountingDate or decimals — so a silent
    revert would parse to nothing rather than fail loudly."""
    calls: list[tuple[str, str, dict]] = []

    class FakeHttp:
        async def request(self, method, url, **kwargs):
            calls.append((method, url, kwargs.get("headers") or {}))
            return FakeResponse(200, json_data=[_meter_item("abc")])

    client = BrunataApiClient("user@example.com", "s3cret", FakeHttp())
    client._access_token = "T"
    client._token_type = "Bearer"
    client._expires_at = time.time() + 300
    client._meter_types = METER_TYPES
    client._measurement_units = MEASUREMENT_UNITS
    client._lookup_tables_loaded = True

    meters = await client.async_get_meters()

    assert set(meters) == {"abc"}
    method, url, headers = calls[0]
    assert method == "GET"
    assert url.endswith("/consumer/metersforconsumer")
    assert headers["Authorization"] == "Bearer T"
    assert headers["Referer"] == REFERER_URL


async def test_stale_token_on_the_locale_endpoint_retries_with_a_fresh_login():
    """The 401 retry used to live inside the meters call alone. A stale token
    on the locale resource therefore fell through to _payload(), surfaced as
    "invalid JSON", and — because the lookup tables are fetched once per
    client — repeated on every poll until something else happened to fix it."""
    responses = [
        FakeResponse(401, json_data=None),
        FakeResponse(200, json_data={"mappers": {"meterType": ["a"], "measurementUnit": ["b"]}}),
    ]
    logins: list[bool] = []

    class FakeHttp:
        async def request(self, method, url, **kwargs):
            return responses.pop(0)

    client = BrunataApiClient("user@example.com", "s3cret", FakeHttp())

    async def _fake_login(*, force=False):
        logins.append(force)
        client._access_token = "T"
        client._expires_at = time.time() + 300

    client._async_login = _fake_login

    await client._async_ensure_lookup_tables()

    assert logins == [False, True]
    assert client._lookup_tables_loaded is True
    assert client._meter_types == ["a"]


@pytest.mark.parametrize("status", [401, 403])
async def test_a_rejected_token_is_retried_once_with_a_fresh_login(status):
    """A cached token can look locally valid while the server no longer accepts
    it — a revoked Keycloak session, or clock drift.

    403 is checked alongside 401 because the code treats the two identically in
    four places and neither status appeared in a test that exercised this path.
    """
    responses = [FakeResponse(status), FakeResponse(200, json_data=[])]
    logins: list[bool] = []

    class FakeHttp:
        async def request(self, method, url, **kwargs):
            return responses.pop(0)

    client = BrunataApiClient("user@example.com", "s3cret", FakeHttp())

    async def _fake_login(*, force=False):
        logins.append(force)
        client._access_token = "T"
        client._expires_at = time.time() + 300

    client._async_login = _fake_login

    response = await client._async_authenticated_get(
        f"{API_URL}/consumer/metersforconsumer"
    )

    assert response.status_code == 200
    # The retry is a *forced* login: without it, _async_login() would hand back
    # the same cached token the server has just refused.
    assert logins == [False, True]


@pytest.mark.parametrize("status", [401, 403])
async def test_a_token_rejected_after_a_fresh_login_is_an_auth_error(status):
    """Only a rejection on the *second* attempt means the credentials are no
    longer accepted.

    BrunataAuthError is what the coordinator turns into ConfigEntryAuthFailed,
    which is what opens the re-authentication dialog. Nothing reached this
    line before: the retry was covered, its failure was not, so the one path
    that asks the user for a new password was never exercised.
    """
    responses = [FakeResponse(status), FakeResponse(status)]
    logins: list[bool] = []

    class FakeHttp:
        async def request(self, method, url, **kwargs):
            return responses.pop(0)

    client = BrunataApiClient("user@example.com", "s3cret", FakeHttp())

    async def _fake_login(*, force=False):
        logins.append(force)
        client._access_token = "T"
        client._expires_at = time.time() + 300

    client._async_login = _fake_login

    with pytest.raises(BrunataAuthError, match="even after a fresh login"):
        await client._async_authenticated_get(
            f"{API_URL}/consumer/metersforconsumer"
        )

    assert logins == [False, True]


async def test_client_diagnostics_reports_tokens_without_quoting_them():
    """An access token is a working credential for as long as it lives, and a
    diagnostics file gets attached to public issues. The decision about what is
    safe to publish belongs on the client, next to the fields."""
    client = BrunataApiClient("user@example.com", "s3cret", object())
    client._access_token = "secret-access-token"
    client._refresh_token = "secret-refresh-token"
    client._meter_types = ["Collector", "Radiator"]
    client._measurement_units = ["undefined", "units"]
    client._lookup_tables_loaded = True

    report = client.diagnostics()

    assert report["has_access_token"] is True
    assert report["has_refresh_token"] is True
    assert "secret-access-token" not in str(report)
    assert "secret-refresh-token" not in str(report)
    assert report["lookup_tables_loaded"] is True
    assert report["meter_types"] == ["Collector", "Radiator"]

    # Copied, not handed out: the caller must not be able to mutate the table
    # the parser resolves meter types against.
    report["meter_types"].append("Injected")
    assert client._meter_types == ["Collector", "Radiator"]


@pytest.mark.parametrize(
    "mappers",
    [
        {},
        {"meterType": [], "measurementUnit": ["units"]},
        {"meterType": ["Collector", "Radiator"], "measurementUnit": []},
        {"meterType": [], "measurementUnit": []},
        {"meterType": None, "measurementUnit": None},
    ],
)
async def test_empty_lookup_tables_fail_the_update_instead_of_creating_wrong_units(
    mappers,
):
    """An empty table is as unusable as a missing one, and continuing costs
    more than failing.

    With no table every meter is named and united by its raw code — "8" where
    the user had "m³" — and Home Assistant treats a changed unit on an existing
    sensor as a new series, discarding the long term statistics behind the old
    one. That cannot be undone afterwards. Failing the update costs one poll:
    the coordinator turns BrunataApiError into UpdateFailed and the sensors
    keep the values they already have.

    This used to be a warning followed by carrying on.
    """

    class FakeHttp:
        async def request(self, method, url, **kwargs):
            return FakeResponse(200, json_data={"mappers": mappers})

    client = BrunataApiClient("user@example.com", "s3cret", FakeHttp())
    client._access_token = "T"
    client._expires_at = time.time() + 300

    with pytest.raises(BrunataApiError, match="cannot be resolved"):
        await client._async_ensure_lookup_tables()

    # Not marked as loaded, so the next poll fetches the resource again rather
    # than serving empty tables for the life of the client. The flag exists to
    # stop a *loaded* table being re-fetched; there is nothing loaded here.
    assert client._lookup_tables_loaded is False
    assert client._meter_types == []
    assert client._measurement_units == []


@pytest.mark.parametrize("payload", [[], "text", None])
async def test_unexpected_locale_payload_is_a_clean_error(payload):
    """The coordinator translates BrunataApiError into a retry. An
    AttributeError from assuming the payload is a dict would surface as an
    unhandled exception instead."""

    class FakeHttp:
        async def request(self, method, url, **kwargs):
            return FakeResponse(200, json_data=payload)

    client = BrunataApiClient("user@example.com", "s3cret", FakeHttp())
    client._access_token = "T"
    client._expires_at = time.time() + 300

    with pytest.raises(BrunataApiError):
        await client._async_ensure_lookup_tables()

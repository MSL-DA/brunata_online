"""Tests for the API layer's response handling.

These paths decide whether a user sees stale-but-working sensors, a temporary
failure, or a re-authentication prompt. They previously lived inside the
coordinator alongside the fetch logic and had no coverage at all, which is how
the 429 back-off ended up being dead code for months: it looked correct in
review and nothing exercised it.
"""

from datetime import date

import time

import pytest

from custom_components.brunata.api import (
    METERS_URL,
    BrunataApiClient,
    BrunataApiError,
    BrunataAuthError,
    _parse_meters,
    _payload,
)


class FakeResponse:
    def __init__(self, status_code=200, *, json_data=None, raises=None):
        self.status_code = status_code
        self._json_data = json_data
        self._raises = raises

    def json(self):
        if self._raises is not None:
            raise self._raises
        return self._json_data


METER_TYPES = ["Pulse Collector", "Radiator", "Water", "Energy"]
# Index 8 is what live water meters report; the gaps stand in for units this
# account does not use.
MEASUREMENT_UNITS = ["", "units", "", "", "", "", "", "", "m3"]


def _parse(payload):
    return _parse_meters(
        payload, meter_types=METER_TYPES, measurement_units=MEASUREMENT_UNITS
    )


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
    assert meter.unit == "m3"
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
    [(1, 1, "Radiator", "units"), (2, 8, "Water", "m3")],
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


@pytest.mark.parametrize(("field", "code"), [("meterType", 1), ("unit", 21)])
def test_null_table_entries_fall_back_to_the_raw_code(field, code):
    """Brunata's live tables contain null entries — 7 of 28 meter types and 34
    of 96 units are reserved slots. Returning the None would put it straight
    into BrunataMeter.meter_type, and the sensor platform would die on
    meter_type.lower(), taking every entity with it.

    A supported meterType is used rather than an arbitrary index, because
    anything outside SUPPORTED_METER_TYPES is dropped before the table is
    consulted at all; this test is about the table, not about the allowlist."""
    types = ["Collector", None, "Water"] + [None] * 25
    units = ["undefined", "units"] + [None] * 94

    item = _meter_item("abc")
    item[field] = code

    meter = _parse_meters(
        [item], meter_types=types, measurement_units=units
    )["abc"]
    assert isinstance(meter.meter_type, str)
    assert isinstance(meter.unit, str)
    assert (meter.meter_type if field == "meterType" else meter.unit) == str(code)


@pytest.mark.parametrize("code", [1, 2])
def test_supported_meter_types_are_parsed(code):
    """The two codes read off live account data: 1 = heat cost allocator
    (radiator), 2 = water. Nothing else is on the list, because nothing else
    has been read off a real account — see SUPPORTED_METER_TYPES."""
    item = _meter_item("abc")
    item["meterType"] = code

    assert "abc" in _parse([item])


@pytest.mark.parametrize("code", [1, 2])
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
    still there when the name falls back to the raw code."""
    item = _meter_item("abc")
    item["meterType"] = 1

    meter = _parse_meters([item], meter_types=[], measurement_units=[])["abc"]

    assert meter.meter_type == "1"
    assert meter.meter_type_code == 1


def test_a_meter_type_given_as_a_string_is_carried_as_an_int():
    """`unit` arrives as a string in this payload, so meterType could too. The
    code must be comparable to the ints in SUPPORTED_METER_TYPES and
    ANNUAL_RESET_METER_TYPES either way."""
    item = _meter_item("abc")
    item["meterType"] = "1"

    assert _parse([item])["abc"].meter_type_code == 1


@pytest.mark.parametrize("code", [0, 3, 4, 5, 9, 17, 27, -1, 99])
def test_unsupported_meter_types_never_become_entities(code):
    """The safety boundary. Brunata's portal can carry leak and smoke
    detectors, and this integration polls once an hour over a cloud API — up
    to 59 minutes and 30 seconds can pass between an event and Home Assistant
    hearing about it.

    An entity invites an automation, and that automation would be dangerously
    slow. Nothing downstream can undo it once the entity exists, so the meter
    has to be dropped here. If this test ever needs relaxing, that is the
    moment to think very hard about why.

    3 is in this list on purpose. It is widely assumed to be an energy meter,
    but that has never been read off a real account, and an assumption does
    not belong on a safety boundary. It moves to the supported list when
    somebody posts the log line that proves it."""
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
    detector["meterType"] = 26

    assert sorted(_parse([water, detector])) == ["water"]


def test_trailing_whitespace_in_table_entries_is_stripped():
    """The live table has entries with a trailing space; left unstripped it
    would end up in the device name verbatim."""
    item = _meter_item("abc")
    item["meterType"] = 1

    meter = _parse_meters(
        [item], meter_types=["", "Radiator "], measurement_units=MEASUREMENT_UNITS
    )["abc"]
    assert meter.meter_type == "Radiator"


@pytest.mark.parametrize("code", [-1, "-1", -99])
def test_a_negative_code_falls_back_instead_of_wrapping_round_the_table(code):
    """Python indexes lists from both ends. Brunata does not.

    `table[-1]` does not raise IndexError — it returns the *last* entry, so a
    negative unit code resolved to a real, wrong unit: wrong device class,
    wrong state class, and long term statistics that look right and are not.
    It happened silently, because _warn_unresolved_code() only fires when the
    lookup fails.

    meterType is shielded by the allowlist (-1 is not in SUPPORTED_METER_TYPES);
    unit is not, so it is the field under test here.
    """
    item = _meter_item("abc")
    item["unit"] = code

    meter = _parse_meters(
        [item],
        meter_types=METER_TYPES,
        measurement_units=["undefined", "units", "m3", "THE-LAST-ENTRY"],
    )["abc"]

    assert meter.unit == str(code)


def test_unknown_codes_fall_back_to_the_raw_value():
    """An index past the end of the table must not take down the platform —
    passing a code through as-is once crashed every entity on
    meter.meter_type.lower(), not just the unrecognised one.

    A short meter type table is used so that meterType 2 — supported, so it
    reaches the lookup — lands past the end of it."""
    item = _meter_item("abc")
    item["meterType"] = 2
    item["unit"] = 99

    meter = _parse_meters(
        [item], meter_types=["Collector", "Radiator"],
        measurement_units=MEASUREMENT_UNITS,
    )["abc"]
    assert meter.meter_type == "2"
    assert meter.unit == "99"


def test_missing_lookup_tables_do_not_crash():
    """If the locale resource ever fails to load, entities should still be
    created rather than the platform failing outright."""
    meter = _parse_meters([_meter_item("abc")])["abc"]
    assert meter.meter_type == "2"
    assert meter.unit == "8"


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
    assert headers["Referer"] == METERS_URL


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

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

    meter = _parse(item and [item])["abc"]
    assert meter.meter_type == expected_type
    assert meter.unit == expected_unit


@pytest.mark.parametrize(("field", "code"), [("meterType", 20), ("unit", 21)])
def test_null_table_entries_fall_back_to_the_raw_code(field, code):
    """Brunata's live tables contain null entries — 7 of 28 meter types and 34
    of 96 units are reserved slots. Returning the None would put it straight
    into BrunataMeter.meter_type, and the sensor platform would die on
    meter_type.lower(), taking every entity with it."""
    types = ["Collector", "Radiator", "Water"] + [None] * 25
    units = ["undefined", "units"] + [None] * 94

    item = _meter_item("abc")
    item[field] = code

    meter = _parse_meters(
        [item], meter_types=types, measurement_units=units
    )["abc"]
    assert isinstance(meter.meter_type, str)
    assert isinstance(meter.unit, str)
    assert (meter.meter_type if field == "meterType" else meter.unit) == str(code)


def test_trailing_whitespace_in_table_entries_is_stripped():
    """Two live entries carry one ("Electricity ", "Carbon dioxide "), and the
    meter type becomes the device name."""
    item = _meter_item("abc")
    item["meterType"] = 1

    meter = _parse_meters(
        [item], meter_types=["", "Electricity "], measurement_units=MEASUREMENT_UNITS
    )["abc"]
    assert meter.meter_type == "Electricity"


def test_unknown_codes_fall_back_to_the_raw_value():
    """An index past the end of the table must not take down the platform —
    passing a code through as-is once crashed every entity on
    meter.meter_type.lower(), not just the unrecognised one."""
    item = _meter_item("abc")
    item["meterType"] = 99
    item["unit"] = 99

    meter = _parse([item])["abc"]
    assert meter.meter_type == "99"
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


@pytest.mark.parametrize("decimals", [None, "3", 1.5])
def test_non_integer_decimals_is_ignored(decimals):
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

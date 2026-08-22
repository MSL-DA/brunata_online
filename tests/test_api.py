"""Tests for the API layer's response handling.

These paths decide whether a user sees stale-but-working sensors, a temporary
failure, or a re-authentication prompt. They previously lived inside the
coordinator alongside the fetch logic and had no coverage at all, which is how
the 429 back-off ended up being dead code for months: it looked correct in
review and nothing exercised it.
"""

from datetime import date

import pytest

from custom_components.brunata.api import (
    METERS_URL,
    BrunataApiClient,
    BrunataApiError,
    BrunataAuthError,
    BrunataConnectionError,
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
MEASUREMENT_UNITS = ["", "units", "m3", "kWh"]


def _parse(payload):
    return _parse_meters(
        payload, meter_types=METER_TYPES, measurement_units=MEASUREMENT_UNITS
    )


def _meter_item(meter_id="12345", *, super_allocation="HEAT", reading=True, value=42.0):
    item = {
        "meter": {
            "meterId": meter_id,
            "meterNo": f"M{meter_id}",
            "meterType": 1,
            "unit": 1,
            "superAllocationUnit": super_allocation,
        }
    }
    if reading:
        item["reading"] = {"value": value, "readingDate": "2026-01-01"}
    return item


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
    assert meter.meter_type == "Radiator"
    assert meter.unit == "units"
    assert meter.value == 13.5
    assert meter.reading_date == date(2026, 1, 1)


def test_meter_without_a_reading_has_no_value():
    """A meter can exist before it has reported. It must still appear, so the
    entity is created and can pick up a value later."""
    meters = _parse([_meter_item("abc", reading=False)])

    assert meters["abc"].value is None
    assert meters["abc"].reading_date is None


def test_meters_without_super_allocation_unit_are_skipped():
    meters = _parse(
        [_meter_item("keep"), _meter_item("drop", super_allocation=None)]
    )
    assert set(meters) == {"keep"}


def test_meter_without_an_id_is_skipped():
    """Previously str(None) produced a meter keyed "None", and an entity with
    unique_id brunata_None_consumption."""
    item = _meter_item("x")
    item["meter"]["meterId"] = None

    assert _parse([item]) == {}


def test_malformed_entries_are_skipped_not_fatal():
    """One bad row must not cost the user every other meter."""
    meters = _parse(
        ["not a dict", {}, {"meter": "not a dict"}, _meter_item("good")]
    )
    assert set(meters) == {"good"}


def test_unparseable_reading_date_leaves_the_value_intact():
    item = _meter_item("abc")
    item["reading"]["readingDate"] = "not-a-date"

    meter = _parse([item])["abc"]
    assert meter.value == 42.0
    assert meter.reading_date is None


def test_timestamp_reading_date_is_accepted():
    item = _meter_item("abc")
    item["reading"]["readingDate"] = "2026-01-01T04:00:00Z"

    assert _parse([item])["abc"].reading_date == date(2026, 1, 1)


@pytest.mark.parametrize(
    ("type_code", "unit_code", "expected_type", "expected_unit"),
    [(1, 1, "Radiator", "units"), (2, 2, "Water", "m3")],
)
def test_codes_are_resolved_through_the_lookup_tables(
    type_code, unit_code, expected_type, expected_unit
):
    """The payload carries indices, not names. Resolving them wrong is not
    cosmetic: the device is named after the type, and Home Assistant suppresses
    a sensor's long term statistics when its unit changes, so water meters
    silently stopped recording."""
    item = _meter_item("abc")
    item["meter"]["meterType"] = type_code
    item["meter"]["unit"] = unit_code

    meter = _parse(item and [item])["abc"]
    assert meter.meter_type == expected_type
    assert meter.unit == expected_unit


def test_unknown_codes_fall_back_to_the_raw_value():
    """An index past the end of the table must not take down the platform —
    passing a code through as-is once crashed every entity on
    meter.meter_type.lower(), not just the unrecognised one."""
    item = _meter_item("abc")
    item["meter"]["meterType"] = 99
    item["meter"]["unit"] = 99

    meter = _parse([item])["abc"]
    assert meter.meter_type == "99"
    assert meter.unit == "99"


def test_missing_lookup_tables_do_not_crash():
    """If the locale resource ever fails to load, entities should still be
    created rather than the platform failing outright."""
    meter = _parse_meters([_meter_item("abc")])["abc"]
    assert meter.meter_type == "1"


def test_non_list_payload_raises_api_error():
    with pytest.raises(BrunataApiError, match="Expected a list"):
        _parse({"unexpected": True})


# --- placements ----------------------------------------------------------
#
# _async_get_placements() is a second HTTP call, to a different endpoint, whose
# result becomes the device name a user sees. It is deliberately best-effort:
# a failure must degrade to "no placements" rather than fail the whole update,
# because a meter without a placement still has a usable name from its type and
# ID. That makes it exactly the kind of code that can rot unnoticed — nothing
# breaks loudly when it stops working — so each branch is pinned here.


class _PlacementClient:
    """A BrunataApiClient whose only live wiring is the HTTP call."""

    def __init__(self, response=None, raises=None):
        self._response = response
        self._raises = raises
        self.requests = []
        self._token_type = "Bearer"
        self._access_token = "token"

    async def _async_request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        if self._raises is not None:
            raise self._raises
        return self._response

    async def get(self):
        return await BrunataApiClient._async_get_placements(self)


async def test_placements_are_keyed_by_meter_id():
    client = _PlacementClient(
        FakeResponse(
            json_data=[
                {"meterId": 12345, "placement": "Bad/Køkken (Koldt)"},
                {"meterId": "67890", "placement": "Stue"},
            ]
        )
    )

    # Numeric IDs are stringified, because _parse_meters() keys its dict on
    # str(meterId) and the two have to line up for the lookup to hit.
    assert await client.get() == {
        "12345": "Bad/Køkken (Koldt)",
        "67890": "Stue",
    }


async def test_placements_call_the_right_endpoint_with_auth():
    client = _PlacementClient(FakeResponse(json_data=[]))
    await client.get()

    method, url, kwargs = client.requests[0]
    assert method == "GET"
    assert url.endswith("/consumer/metersforconsumer")
    # Brunata's bot protection rejects API calls that don't look like they came
    # from the web app, so the Referer matters as much as the token.
    assert kwargs["headers"]["Authorization"] == "Bearer token"
    assert kwargs["headers"]["Referer"] == METERS_URL


@pytest.mark.parametrize(
    "item",
    [
        {"placement": "Stue"},                       # no meterId
        {"meterId": 12345},                          # no placement
        {"meterId": 12345, "placement": ""},         # empty placement
        {"meterId": 12345, "placement": 42},         # placement not a string
        "not an object",
    ],
)
async def test_unusable_placement_entries_are_skipped(item):
    """A meter with no usable label falls back to type+ID naming, so a bad
    entry must be dropped rather than producing a device called 'None'."""
    client = _PlacementClient(FakeResponse(json_data=[item]))
    assert await client.get() == {}


async def test_usable_entries_survive_alongside_unusable_ones():
    client = _PlacementClient(
        FakeResponse(json_data=[{"meterId": 1}, {"meterId": 2, "placement": "Stue"}])
    )
    assert await client.get() == {"2": "Stue"}


@pytest.mark.parametrize("payload", [None, 42, {"unexpected": True}])
async def test_non_list_placement_payload_is_not_fatal(payload):
    """The type guard has to run before the loop, not be implied by it.

    A dict would survive being iterated — the loop would just skip its string
    keys — so a dict alone does not prove the guard exists. None and an int
    raise TypeError the moment the loop starts, which is what pins it.
    """
    client = _PlacementClient(FakeResponse(json_data=payload))
    assert await client.get() == {}


@pytest.mark.parametrize(
    "error",
    [
        BrunataConnectionError("network down"),
        BrunataApiError("server error", 500),
        BrunataAuthError("token rejected"),
    ],
)
async def test_placement_failures_degrade_to_no_placements(error):
    """Every BrunataError is swallowed. This call runs after the readings are
    already in hand, so failing the update over a missing label would throw
    away good data — including the auth error, which the readings call has its
    own retry for."""
    client = _PlacementClient(raises=error)
    assert await client.get() == {}


async def test_placement_error_body_degrades_to_no_placements():
    """Brunata reports some errors with HTTP 200 and an error body, which
    _payload() turns into an exception from inside the try block."""
    client = _PlacementClient(
        FakeResponse(json_data={"errorCode": "WB_X", "errorMessage": "nope"})
    )
    assert await client.get() == {}

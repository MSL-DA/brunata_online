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


def _meter_item(meter_id="12345", *, super_allocation="HEAT", reading=True, value=42.0):
    item = {
        "meter": {
            "meterId": meter_id,
            "meterNo": f"M{meter_id}",
            "meterType": "Radiator",
            "meterUnit": "units",
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
    meters = _parse_meters([_meter_item("abc", value=13.5)])

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
    meters = _parse_meters([_meter_item("abc", reading=False)])

    assert meters["abc"].value is None
    assert meters["abc"].reading_date is None


def test_meters_without_super_allocation_unit_are_skipped():
    meters = _parse_meters(
        [_meter_item("keep"), _meter_item("drop", super_allocation=None)]
    )
    assert set(meters) == {"keep"}


def test_meter_without_an_id_is_skipped():
    """Previously str(None) produced a meter keyed "None", and an entity with
    unique_id brunata_None_consumption."""
    item = _meter_item("x")
    item["meter"]["meterId"] = None

    assert _parse_meters([item]) == {}


def test_malformed_entries_are_skipped_not_fatal():
    """One bad row must not cost the user every other meter."""
    meters = _parse_meters(
        ["not a dict", {}, {"meter": "not a dict"}, _meter_item("good")]
    )
    assert set(meters) == {"good"}


def test_unparseable_reading_date_leaves_the_value_intact():
    item = _meter_item("abc")
    item["reading"]["readingDate"] = "not-a-date"

    meter = _parse_meters([item])["abc"]
    assert meter.value == 42.0
    assert meter.reading_date is None


def test_timestamp_reading_date_is_accepted():
    item = _meter_item("abc")
    item["reading"]["readingDate"] = "2026-01-01T04:00:00Z"

    assert _parse_meters([item])["abc"].reading_date == date(2026, 1, 1)


def test_numeric_meter_codes_do_not_crash():
    """Brunata's v2 payload returns meterType and meterUnit as numeric codes.
    Passing them through as-is crashed the whole sensor platform on
    meter.meter_type.lower(), so every entity was lost, not just the naming."""
    item = _meter_item("abc")
    item["meter"]["meterType"] = 3
    item["meter"]["meterUnit"] = 7

    meter = _parse_meters([item])["abc"]
    assert meter.meter_type == "3"
    assert meter.unit == "7"


def test_non_list_payload_raises_api_error():
    with pytest.raises(BrunataApiError, match="Expected a list"):
        _parse_meters({"unexpected": True})

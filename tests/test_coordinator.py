"""Tests for the coordinator's error handling.

Every branch here decides whether the user sees stale-but-working sensors, a
temporary failure, or a re-authentication prompt. None of them had coverage,
which is how the 429 back-off ended up being dead code for months: it looked
correct in review and nothing exercised it.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.brunata import BrunataDataUpdateCoordinator
from custom_components.brunata.const import DOMAIN


class FakeResponse:
    """Minimal stand-in for the httpx.Response the API wrapper returns."""

    def __init__(self, status_code=200, *, json_data=None, headers=None, text="", raises=None):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text
        self._json_data = json_data
        self._raises = raises

    def json(self):
        if self._raises is not None:
            raise self._raises
        return self._json_data


def _meter_payload(meter_id="12345", *, super_allocation="HEAT", reading=True):
    item = {
        "meter": {
            "meterId": meter_id,
            "meterNo": f"M{meter_id}",
            "superAllocationUnit": super_allocation,
        }
    }
    if reading:
        item["reading"] = {"value": 42.0, "readingDate": "2026-01-01"}
    return item


@pytest.fixture
def coordinator(hass: HomeAssistant, mock_brunata_client):
    """A coordinator wired to the mocked client, without running setup."""
    entry = MockConfigEntry(
        domain=DOMAIN, data={"email": "test@example.com", "password": "password123"}
    )
    entry.add_to_hass(hass)
    return BrunataDataUpdateCoordinator(hass, entry, mock_brunata_client)


async def test_rate_limit_fails_the_update(coordinator, mock_brunata_client):
    """429 must fail the cycle rather than return empty data — returning {}
    would silently remove every sensor's value."""
    mock_brunata_client.api_wrapper = AsyncMock(
        return_value=FakeResponse(429, headers={"Retry-After": "120"})
    )

    with pytest.raises(UpdateFailed, match="429"):
        await coordinator._async_update_data()


async def test_server_error_fails_the_update(coordinator, mock_brunata_client):
    mock_brunata_client.api_wrapper = AsyncMock(return_value=FakeResponse(503))

    with pytest.raises(UpdateFailed, match="server error"):
        await coordinator._async_update_data()


async def test_missing_endpoint_fails_distinctly(coordinator, mock_brunata_client):
    """404 is neither an auth problem nor transient, so it must not be
    confused with either."""
    mock_brunata_client.api_wrapper = AsyncMock(return_value=FakeResponse(404))

    with pytest.raises(UpdateFailed, match="404"):
        await coordinator._async_update_data()


async def test_auth_error_in_body_triggers_reauth(coordinator, mock_brunata_client):
    """Brunata reports some auth failures with HTTP 200 and an error body
    instead of a proper 401."""
    mock_brunata_client.api_wrapper = AsyncMock(
        return_value=FakeResponse(
            200,
            json_data={
                "errorCode": "WB_WEBSERVICES_0011",
                "errorMessage": "Not authorized",
            },
        )
    )

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_other_error_in_body_is_not_an_auth_failure(coordinator, mock_brunata_client):
    """A non-auth error body must not prompt the user for credentials."""
    mock_brunata_client.api_wrapper = AsyncMock(
        return_value=FakeResponse(
            200, json_data={"errorCode": "WB_SOMETHING_ELSE", "errorMessage": "Boom"}
        )
    )

    with pytest.raises(UpdateFailed, match="WB_SOMETHING_ELSE"):
        await coordinator._async_update_data()


async def test_unparseable_json_keeps_last_known_meters(coordinator, mock_brunata_client):
    mock_brunata_client._meters = {"12345": MagicMock()}
    mock_brunata_client.api_wrapper = AsyncMock(
        return_value=FakeResponse(200, raises=ValueError("no json"), text="<html>")
    )

    result = await coordinator._async_update_data()
    assert set(result) == {"12345"}


async def test_unexpected_payload_shape_keeps_last_known_meters(
    coordinator, mock_brunata_client
):
    """A dict without error fields where a list was expected."""
    mock_brunata_client._meters = {"12345": MagicMock()}
    mock_brunata_client.api_wrapper = AsyncMock(
        return_value=FakeResponse(200, json_data={"unexpected": True})
    )

    result = await coordinator._async_update_data()
    assert set(result) == {"12345"}


async def test_network_failure_keeps_last_known_values(coordinator, mock_brunata_client):
    """A short outage must not blank the sensors, or their statistics get a
    gap for every hour Brunata is unreachable."""
    previous = {"12345": MagicMock()}
    coordinator.data = previous
    mock_brunata_client.api_wrapper = AsyncMock(side_effect=ConnectionError("down"))

    assert await coordinator._async_update_data() is previous


async def test_network_failure_on_first_update_fails(coordinator, mock_brunata_client):
    """With no previous data there is nothing to keep, so the failure must be
    surfaced — otherwise setup would 'succeed' with zero sensors."""
    coordinator.data = None
    mock_brunata_client.api_wrapper = AsyncMock(side_effect=ConnectionError("down"))

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_library_unbound_local_bug_is_treated_as_a_network_error(
    coordinator, mock_brunata_client
):
    """brunata_api reads an unassigned `response` variable after catching a
    ConnectError internally. Only that exact case counts as a network error."""
    previous = {"12345": MagicMock()}
    coordinator.data = previous
    mock_brunata_client.api_wrapper = AsyncMock(
        side_effect=UnboundLocalError(
            "cannot access local variable 'response' where it is not associated"
        )
    )

    assert await coordinator._async_update_data() is previous


async def test_meters_without_super_allocation_unit_are_skipped(
    coordinator, mock_brunata_client
):
    mock_brunata_client._meters = {}
    mock_brunata_client.api_wrapper = AsyncMock(
        return_value=FakeResponse(
            200,
            json_data=[
                _meter_payload("keep"),
                _meter_payload("drop", super_allocation=None),
            ],
        )
    )

    result = await coordinator._async_update_data()
    assert set(result) == {"keep"}


async def test_setup_hook_loads_mappers_once(coordinator, mock_brunata_client):
    """_init_mappers loads static metadata, so it belongs in _async_setup and
    must not run on every hourly poll."""
    await coordinator._async_setup()
    assert mock_brunata_client._init_mappers.await_count == 1

    mock_brunata_client.api_wrapper = AsyncMock(
        return_value=FakeResponse(200, json_data=[_meter_payload()])
    )
    await coordinator._async_update_data()
    assert mock_brunata_client._init_mappers.await_count == 1


async def test_setup_hook_reports_connection_failure_as_update_failed(
    coordinator, mock_brunata_client
):
    """Raised as UpdateFailed so HA turns it into ConfigEntryNotReady with its
    own backoff, rather than logging an unhandled traceback."""
    mock_brunata_client._init_mappers = AsyncMock(side_effect=ConnectionError("down"))

    with pytest.raises(UpdateFailed):
        await coordinator._async_setup()

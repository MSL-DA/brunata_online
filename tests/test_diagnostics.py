"""Test Brunata diagnostics.

The point of diagnostics is that a user can attach it to an issue without
having to think about what is in it. That only holds if the redaction is
verified — a leaked password in a public GitHub issue is not recoverable.
"""

from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.brunata.api import BrunataApiError
from custom_components.brunata.const import DOMAIN
from custom_components.brunata.diagnostics import (
    async_get_config_entry_diagnostics,
)


async def _setup(hass: HomeAssistant, mock_brunata_client, mock_meter):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"email": "test@example.com", "password": "password123"},
    )
    entry.add_to_hass(hass)

    mock_brunata_client.async_get_meters = AsyncMock(
        return_value={"12345": mock_meter}
    )
    # diagnostics.py asks the client for this rather than reading five private
    # attributes off it; what the report may contain is decided in api.py, and
    # tested there by test_client_diagnostics_reports_tokens_without_quoting_them.
    mock_brunata_client.diagnostics.return_value = {
        "lookup_tables_loaded": True,
        "meter_types": ["Collector", "Radiator", "Water"],
        "measurement_units": ["undefined", "units", "m3"],
        "has_access_token": True,
        "has_refresh_token": True,
    }

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_diagnostics_redacts_the_credentials(
    hass: HomeAssistant, mock_brunata_client, mock_meter
):
    """The email and password must never reach the downloaded file."""
    entry = await _setup(hass, mock_brunata_client, mock_meter)

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["entry"]["data"]["email"] == "**REDACTED**"
    assert result["entry"]["data"]["password"] == "**REDACTED**"
    assert "test@example.com" not in str(result)
    assert "password123" not in str(result)


async def test_diagnostics_reports_the_client_state_verbatim(
    hass: HomeAssistant, mock_brunata_client, mock_meter
):
    """The api section is whatever BrunataApiClient.diagnostics() returned.

    That the tokens themselves never appear in it is the client's guarantee and
    is tested there; what matters here is that this module passes the report
    through and does not go looking for anything else on its own."""
    entry = await _setup(hass, mock_brunata_client, mock_meter)

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["api"] == mock_brunata_client.diagnostics.return_value
    mock_brunata_client.diagnostics.assert_called_once_with()


async def test_diagnostics_reports_the_http_status_of_the_last_failure(
    hass: HomeAssistant, mock_brunata_client, mock_meter
):
    """Separately from the message, so a report can be read without parsing
    prose: 429 means back off, 500 means Brunata is having a bad day."""
    entry = await _setup(hass, mock_brunata_client, mock_meter)
    coordinator = entry.runtime_data

    mock_brunata_client.async_get_meters.side_effect = BrunataApiError(
        "Brunata rate limit reached (429)", 429
    )
    await coordinator.async_refresh()

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["coordinator"]["last_update_success"] is False
    assert result["coordinator"]["last_exception_status"] == 429


async def test_diagnostics_status_is_none_when_there_is_no_status(
    hass: HomeAssistant, mock_brunata_client, mock_meter
):
    """Not every failure has an HTTP status — a login flow that changed shape
    has none. The key must still be present rather than missing."""
    entry = await _setup(hass, mock_brunata_client, mock_meter)

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["coordinator"]["last_exception_status"] is None


async def test_diagnostics_redacts_the_meter_number(
    hass: HomeAssistant, mock_brunata_client, mock_meter
):
    """The meter number identifies a device at an address. Redacted rather
    than dropped, so a change to it is still visible."""
    entry = await _setup(hass, mock_brunata_client, mock_meter)

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["meters"][0]["meter_no"] == "**REDACTED**"
    assert mock_meter.meter_no not in str(result)


async def test_diagnostics_includes_what_faults_have_needed(
    hass: HomeAssistant, mock_brunata_client, mock_meter
):
    """Each of these has been the answer to a real fault: the lookup tables
    when meters were named after numbers, the raw unit when statistics were
    suppressed, and the coordinator state when values went stale."""
    entry = await _setup(hass, mock_brunata_client, mock_meter)

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["api"]["lookup_tables_loaded"] is True
    assert result["api"]["meter_types"] == ["Collector", "Radiator", "Water"]
    assert result["api"]["measurement_units"] == ["undefined", "units", "m3"]

    assert result["coordinator"]["last_update_success"] is True
    assert result["coordinator"]["meter_count"] == 1

    meter = result["meters"][0]
    assert meter["meter_id"] == mock_meter.meter_id
    assert meter["meter_type"] == mock_meter.meter_type
    assert meter["unit"] == mock_meter.unit
    assert meter["value"] == mock_meter.value


async def test_diagnostics_serialises_dates_as_strings(
    hass: HomeAssistant, mock_brunata_client, mock_meter
):
    """date and datetime are not JSON. Left as objects they would either fail
    to serialise or arrive as something unreadable."""
    entry = await _setup(hass, mock_brunata_client, mock_meter)

    result = await async_get_config_entry_diagnostics(hass, entry)
    meter = result["meters"][0]

    assert meter["reading_date"] == mock_meter.reading_date.isoformat()
    assert meter["mounting_date"] == mock_meter.mounting_date.isoformat()

"""Test Brunata diagnostics.

The point of diagnostics is that a user can attach it to an issue without
having to think about what is in it. That only holds if the redaction is
verified — a leaked password in a public GitHub issue is not recoverable.
"""

from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

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
    mock_brunata_client._lookup_tables_loaded = True
    mock_brunata_client._meter_types = ["Collector", "Radiator", "Water"]
    mock_brunata_client._measurement_units = ["undefined", "units", "m3"]
    mock_brunata_client._access_token = "secret-access-token"
    mock_brunata_client._refresh_token = "secret-refresh-token"

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


async def test_diagnostics_never_contains_a_token(
    hass: HomeAssistant, mock_brunata_client, mock_meter
):
    """Presence is reported, the token itself is not — an access token is a
    working credential for as long as it lives."""
    entry = await _setup(hass, mock_brunata_client, mock_meter)

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["api"]["has_access_token"] is True
    assert result["api"]["has_refresh_token"] is True
    assert "secret-access-token" not in str(result)
    assert "secret-refresh-token" not in str(result)


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

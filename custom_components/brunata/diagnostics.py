"""Diagnostics support for the Brunata integration.

Downloadable from the integration page in Home Assistant, so a bug report can
arrive with the full picture attached instead of a description of it. Enabling
debug logging, reproducing the fault and pasting the right lines is a lot to
ask of someone who just wants their meters back.

Credentials are redacted. Everything else is included deliberately: the fields
below are the ones that have actually been needed to diagnose faults in this
integration — the lookup tables, the raw meter type and unit codes, and the
coordinator's last error.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant

from . import BrunataConfigEntry
from .api import BrunataApiError

# The email is the account identifier and the password is the account. Neither
# is ever needed to understand a fault.
TO_REDACT = {CONF_EMAIL, CONF_PASSWORD}

# The meter number identifies a physical device installed at an address. It is
# redacted rather than dropped, so its *presence* and any change to it are
# still visible — that is the signal the replacement guard acts on.
TO_REDACT_METER = {"meter_no"}


def _serialise(value: Any) -> Any:
    """Make a value safe for the diagnostics JSON payload."""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _meter_diagnostics(meter: Any) -> dict[str, Any]:
    """Describe one meter, redacted."""
    return async_redact_data(
        {key: _serialise(value) for key, value in asdict(meter).items()},
        TO_REDACT_METER,
    )


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: BrunataConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    client = coordinator.client
    meters = coordinator.data or {}

    return {
        "entry": {
            "version": entry.version,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "coordinator": {
            # A failing update leaves the sensors on their last known values,
            # so "everything looks fine but the numbers are old" and "the API
            # is refusing us" are indistinguishable without this.
            "last_update_success": coordinator.last_update_success,
            "last_exception": (
                repr(coordinator.last_exception)
                if coordinator.last_exception is not None
                else None
            ),
            # The status separately from the message, so a report can be read
            # without parsing prose: 429 means back off, 500 means Brunata is
            # having a bad day, 404 means an endpoint moved. None when the
            # failure had no HTTP status of its own.
            "last_exception_status": (
                coordinator.last_exception.status
                if isinstance(coordinator.last_exception, BrunataApiError)
                else None
            ),
            "meter_count": len(meters),
        },
        # Every meter type and unit is an index into the lookup tables. If they
        # failed to load, every meter is named and united by a bare number, and
        # that is the first thing to check. What goes in here is decided by
        # BrunataApiClient.diagnostics(), next to the fields themselves —
        # notably that tokens are reported as present or absent, never quoted.
        "api": client.diagnostics(),
        "meters": [_meter_diagnostics(meter) for meter in meters.values()],
    }

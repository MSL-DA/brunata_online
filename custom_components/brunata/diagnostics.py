"""Diagnostics support for the Brunata integration.

Downloadable from the integration page in Home Assistant, so a bug report can
arrive with the full picture attached instead of a description of it. Enabling
debug logging, reproducing the fault and pasting the right lines is a lot to
ask of someone who just wants their meters back.

Credentials are redacted. Everything else is included deliberately: the fields
below are the ones that have actually been needed to diagnose faults in this
integration — the lookup tables, the numeric meter type code, the resolved
unit, and the coordinator's last error.

The unit is a resolved name rather than the raw index, because api.py now
skips any meter whose unit does not resolve; a raw code can no longer reach a
BrunataMeter at all. meter_type_code is the one field that is still the number
Brunata sent.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant

from . import BrunataConfigEntry
from .api import BrunataApiError, BrunataMeter, format_date

# The email is the account identifier and the password is the account. Neither
# is ever needed to understand a fault.
TO_REDACT = {CONF_EMAIL, CONF_PASSWORD}

# The meter number identifies a physical device installed at an address. It is
# redacted rather than dropped, so its *presence* and any change to it are
# still visible — that is the signal the replacement guard acts on.
TO_REDACT_METER = {"meter_no"}


def _meter_diagnostics(meter: BrunataMeter) -> dict[str, Any]:
    """Describe one meter, redacted.

    Annotated with the real type rather than Any: asdict() below only works on
    a dataclass instance, so the signature should say so.

    Dates go through api.format_date(), which is the same function sensor.py
    uses for the reading_date attribute. It used to be a private copy here
    under the name _serialise.
    """
    return async_redact_data(
        {key: format_date(value) for key, value in asdict(meter).items()},
        TO_REDACT_METER,
    )


def _api_status(err: BaseException | None) -> int | None:
    """Find the HTTP status behind a coordinator failure.

    The coordinator never holds api.py's exception. _async_update_data()
    translates it — `raise UpdateFailed(str(err)) from err` — so last_exception
    is the translated one and ours is its __cause__. Reading the attribute off
    last_exception directly returns None every time, which is exactly what the
    first version of this did.

    The chain is walked with a seen-set because __cause__ can, in principle,
    form a cycle, and a diagnostics download is the wrong place to hang.
    """
    seen: set[int] = set()
    while err is not None and id(err) not in seen:
        if isinstance(err, BrunataApiError):
            return err.status
        seen.add(id(err))
        err = err.__cause__
    return None


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: BrunataConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry.

    runtime_data is read defensively, the same way async_unload_entry() and
    async_remove_config_entry_device() read it. Whether Home Assistant can
    actually reach this function for an entry that is not loaded has not been
    established — it would mean the download handler skipping the state check —
    but the answer only decides whether the branch below ever runs, not whether
    it belongs. A diagnostics download exists to explain a broken integration,
    and an integration that failed to set up is the case where it is needed
    most; returning a stack trace there would be the worst possible moment for
    one.
    """
    entry_report = {
        "version": entry.version,
        "data": async_redact_data(dict(entry.data), TO_REDACT),
        # No "options" key: this integration has no options flow, so it
        # was reported as {} in every report ever downloaded. A field that
        # cannot carry information costs the reader's attention each time.
    }

    coordinator = getattr(entry, "runtime_data", None)
    if coordinator is None:
        return {
            "entry": entry_report,
            # Named rather than left out, so a reader can tell "the entry was
            # never set up" from "the report is missing a section".
            "loaded": False,
        }

    client = coordinator.client
    meters = coordinator.data or {}

    return {
        "entry": entry_report,
        "loaded": True,
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
            "last_exception_status": _api_status(coordinator.last_exception),
            "meter_count": len(meters),
            # How many entries the last payload held before any filtering.
            # Together with meter_count it says how much was dropped, and a
            # zero here is the one value that means "Brunata told us nothing",
            # which is a different fault from "Brunata says you have no
            # meters".
            "raw_item_count": coordinator.last_parse.raw_item_count,
            # Meters Brunata still reports but whose unit did not resolve. They
            # are absent from "meters" below while their sensors stay available
            # on their last value, so this is the field that explains a sensor
            # that stopped updating without going unavailable.
            "unresolved_unit_meter_ids": sorted(
                coordinator.last_parse.unresolved_unit_meter_ids
            ),
        },
        # Every meter type and unit is an index into the lookup tables. If they
        # failed to load, every meter is named and united by a bare number, and
        # that is the first thing to check. What goes in here is decided by
        # BrunataApiClient.diagnostics(), next to the fields themselves —
        # notably that tokens are reported as present or absent, never quoted.
        "api": client.diagnostics(),
        "meters": [_meter_diagnostics(meter) for meter in meters.values()],
    }

"""Fixtures for Brunata integration tests.

The brunata_api stub that used to live here is gone: the integration no longer
depends on an external library, so there is nothing left to stub. Tests now
patch the integration's own BrunataApiClient, whose surface is small and under
our control.
"""

from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.helpers import device_registry as dr

from custom_components.brunata.api import BrunataMeter, ParseReport
from custom_components.brunata.const import DOMAIN


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
def mock_brunata_client():
    """Patch the API client in both places the integration constructs one."""
    with (
        patch(
            "custom_components.brunata.BrunataApiClient", autospec=True
        ) as setup_client_class,
        patch(
            "custom_components.brunata.config_flow.BrunataApiClient", autospec=True
        ) as config_flow_client_class,
    ):
        client = setup_client_class.return_value
        config_flow_client_class.return_value = client
        # The integration builds its client through the async factory.
        setup_client_class.async_create = AsyncMock(return_value=client)
        config_flow_client_class.async_create = AsyncMock(return_value=client)

        client.async_get_meters = AsyncMock(return_value={})
        client.async_validate_credentials = AsyncMock(return_value=None)
        client.async_close = AsyncMock(return_value=None)
        # raw_item_count=1, not 0. The default models the ordinary case:
        # Brunata answered with a payload, and whatever async_get_meters
        # returns is what the parse made of it. A test that returns {} meters
        # is therefore modelling "the meter was in the payload and got
        # filtered out" — a dismounted meter, say — which is what
        # test_a_device_whose_meter_is_gone_can_be_removed means.
        #
        # Zero means Brunata reported nothing at all, and nothing may be
        # concluded from it. A test that models that sets it explicitly.
        client.last_parse_report = MagicMock(
            return_value=ParseReport(frozenset(), 1)
        )

        yield client


@pytest.fixture
def device_for_meter():
    """Find a meter's device without device_registry.async_get_device().

    That method is deprecated from Home Assistant 2026.9 and *raises* when it
    is called from test code, which has no integration frame; the same call
    from inside the integration only logs a warning. So this is a test-side
    problem with a test-side fix — sensor.py is unaffected until 2027.8.

    async_get_device_by_identifier(), which the deprecation message suggests,
    is not the replacement to reach for here: it arrived in 2026.8, and
    hacs.json declares a floor of 2025.3. async_entries_for_config_entry() is
    not deprecated and has been in Home Assistant far longer, so it works on
    both sides of that line and the floor stays where it is.

    It lives here rather than in a test module because test_init.py and
    test_sensor.py both need it, and it stood in both of them word for word —
    including this explanation. Two copies of a reasoned exception are two
    chances for one of them to be updated alone.
    """

    def _find(hass, entry, meter_id: str):
        registry = dr.async_get(hass)
        identifier = (DOMAIN, f"brunata_{meter_id}")
        for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
            if identifier in device.identifiers:
                return device
        return None

    return _find


@pytest.fixture
def mock_meter():
    """A single meter with a reading, as the API layer would return it.

    A water meter, matching the one on the maintainer's own account. It used to
    be meter_type "Heat" with unit "kWh" — a combination Brunata cannot
    produce, and one that SUPPORTED_METER_TYPES now stops before it becomes an
    entity at all. It read as a real type in assertions like
    "Heat (12345) Consumption", which is how an invented example turns into
    something a later reader takes for a reading.
    """
    return BrunataMeter(
        meter_id="12345",
        meter_no="M12345",
        meter_type="Water",
        meter_type_code=2,
        unit="m3",
        value=100.5,
        reading_date=date(2024, 1, 1),
        mounting_date=datetime(2018, 10, 23, 14, 10, tzinfo=UTC),
        decimals=3,
        transmitting=True,
    )

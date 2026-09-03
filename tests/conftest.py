"""Fixtures for Brunata integration tests.

The brunata_api stub that used to live here is gone: the integration no longer
depends on an external library, so there is nothing left to stub. Tests now
patch the integration's own BrunataApiClient, whose surface is small and under
our control.
"""

from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.brunata.api import BrunataMeter, ParseReport


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

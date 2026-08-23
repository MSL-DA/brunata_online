"""Fixtures for Brunata integration tests.

The brunata_api stub that used to live here is gone: the integration no longer
depends on an external library, so there is nothing left to stub. Tests now
patch the integration's own BrunataApiClient, whose surface is small and under
our control.
"""

from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.brunata.api import BrunataMeter


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

        yield client


@pytest.fixture
def mock_meter():
    """A single meter with a reading, as the API layer would return it."""
    return BrunataMeter(
        meter_id="12345",
        meter_no="M12345",
        meter_type="Heat",
        unit="kWh",
        value=100.5,
        reading_date=date(2024, 1, 1),
        mounting_date=datetime(2018, 10, 23, 14, 10, tzinfo=UTC),
        decimals=2,
        transmitting=True,
    )

"""Fixtures for Brunata integration tests."""
import sys
import types
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

# Provide a lightweight local stub for brunata_api so tests can run without the
# real dependency installed.
brunata_api = types.ModuleType("brunata_api")
brunata_api.const = types.ModuleType("brunata_api.const")
brunata_api.const.API_URL = "https://example.com/api"
brunata_api.const.METERS_URL = "https://example.com/meters"

class Client:
    pass

class Meter:
    def __init__(self, client=None, json_meter=None):
        self._meter_id = None

    def add_reading(self, reading):
        pass

class Reading:
    pass

brunata_api.Client = Client
brunata_api.Meter = Meter
brunata_api.Reading = Reading

sys.modules["brunata_api"] = brunata_api
sys.modules["brunata_api.const"] = brunata_api.const

from brunata_api import Meter, Reading

@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield

@pytest.fixture
def mock_brunata_client():
    """Mock a Brunata client."""
    with patch("custom_components.brunata.config_flow.Client", autospec=True) as mock_client_class, \
         patch("custom_components.brunata.Client", autospec=True) as mock_init_client_class:
        
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_init_client_class.return_value = mock_client
        
        # Setup default mock behavior
        mock_client._meters = {}
        # async_unload_entry closes the underlying httpx client, so aclose()
        # has to be awaitable.
        mock_client._session = MagicMock()
        mock_client._session.aclose = AsyncMock()
        # The coordinator's _async_setup() hook runs on every config entry
        # setup — even when _async_update_data is patched out — so these have
        # to be awaitable or setup fails with ConfigEntryNotReady. Individual
        # tests override them where the call itself is what's being asserted.
        mock_client._get_tokens = AsyncMock(return_value=True)
        mock_client._init_mappers = AsyncMock(return_value=None)
        mock_client.api_wrapper = AsyncMock()
        mock_client.get_meters = AsyncMock(return_value={})

        yield mock_client

@pytest.fixture
def mock_meter():
    """Mock a Brunata meter."""
    meter = MagicMock(spec=Meter)
    meter._meter_id = "12345"
    meter.meter_no = "M12345"
    meter.meter_type = "Heat"
    meter.meter_unit = "kWh"
    
    reading = MagicMock(spec=Reading)
    reading.value = 100.5
    reading.date = date(2024, 1, 1)
    
    meter.latest_reading = reading
    return meter

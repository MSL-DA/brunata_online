"""Client for the Brunata Online API.

Everything the integration needs from Brunata lives in this package and has no
third-party package behind it: the Keycloak login, the
``/consumer/metersforconsumer`` call Brunata's own readings page uses, which
carries each meter's reading together with its placement label, and the
``/consumer/consumption`` call that is made only when a heat cost allocator's
reading falls.

The HTTP client is built and owned here, deliberately *not* taken from
``homeassistant.helpers.httpx_client`` — see ``BrunataApiClient.async_create()``,
which also says why ``async_close()`` has to exist.

The modules, and the order they depend on each other in:

    const      Addresses, headers, fixed values. Imports nothing.
    errors     The four error types. Imports nothing.
    models     BrunataMeter and the parse records. Imports nothing.
    http       Reading a response that has arrived. Imports errors.
    auth       The parts of the login that need no network.
    parse      Payloads to objects, lookup tables, dates.
    client     BrunataApiClient — the only module that makes requests.

What this file exports is the package's public surface: the rest of the
integration imports from ``.api`` and nothing deeper. The underscored names in
the modules above are internal to the package, and the tests that exercise them
import them from the module they live in.
"""

from __future__ import annotations

from .client import BrunataApiClient
from .const import (
    API_URL,
    BASE_URL,
    DEFAULT_HEADERS,
    ISSUE_TRACKER_URL,
    KC_AUTHORIZE_URL,
    KC_CLIENT_ID,
    KC_REALM_BASE,
    KC_REDIRECT_URI,
    KC_SCOPE,
    KC_TOKEN_URL,
    LOCALE,
    REFERER_URL,
    REQUEST_TIMEOUT,
    SUPPORTED_METER_TYPES,
    UNDEFINED_UNIT,
)
from .errors import (
    BrunataApiError,
    BrunataAuthError,
    BrunataConnectionError,
    BrunataError,
)
from .models import BrunataMeter, ParsedMeters, ParseReport
from .parse import format_date, parse_reading_date, parse_timestamp

__all__ = [
    "API_URL",
    "BASE_URL",
    "DEFAULT_HEADERS",
    "ISSUE_TRACKER_URL",
    "KC_AUTHORIZE_URL",
    "KC_CLIENT_ID",
    "KC_REALM_BASE",
    "KC_REDIRECT_URI",
    "KC_SCOPE",
    "KC_TOKEN_URL",
    "LOCALE",
    "REFERER_URL",
    "REQUEST_TIMEOUT",
    "SUPPORTED_METER_TYPES",
    "UNDEFINED_UNIT",
    "BrunataApiClient",
    "BrunataApiError",
    "BrunataAuthError",
    "BrunataConnectionError",
    "BrunataError",
    "BrunataMeter",
    "ParseReport",
    "ParsedMeters",
    "format_date",
    "parse_reading_date",
    "parse_timestamp",
]

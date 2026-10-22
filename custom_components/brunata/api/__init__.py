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
    http       Sending a request, and reading what comes back.
    parse      Payloads to objects, lookup tables, dates. No network.
    auth       BrunataAuth — the Keycloak login and the tokens.
    client     BrunataApiClient — fetching Brunata's data.

BrunataApiClient holds a BrunataAuth and asks it for an Authorization header;
it never reads a token itself. Both are given the same HTTP client, because
Keycloak's SSO cookies live on it — the client owns and closes it, the login
only borrows it.

What this file exports is the package's public surface: the rest of the
integration imports from ``.api`` and nothing deeper. The underscored names in
the modules above are internal to the package, and the tests that exercise them
import them from the module they live in.
"""

from __future__ import annotations

from .client import BrunataApiClient
from .const import (
    API_URL,
    AUTH_SCOPE,
    AUTHORIZE_URL,
    BASE_URL,
    CLIENT_ID,
    DEFAULT_HEADERS,
    ISSUE_TRACKER_URL,
    LOCALE,
    OIDC_BASE_URL,
    REDIRECT_URI,
    REFERER_URL,
    REQUEST_TIMEOUT,
    SUPPORTED_METER_TYPES,
    TOKEN_URL,
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
    "AUTHORIZE_URL",
    "AUTH_SCOPE",
    "BASE_URL",
    "CLIENT_ID",
    "DEFAULT_HEADERS",
    "ISSUE_TRACKER_URL",
    "LOCALE",
    "OIDC_BASE_URL",
    "REDIRECT_URI",
    "REFERER_URL",
    "REQUEST_TIMEOUT",
    "SUPPORTED_METER_TYPES",
    "TOKEN_URL",
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

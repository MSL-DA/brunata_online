"""Client for the Brunata Online API.

This replaces the external ``brunata-api`` package, which targeted Brunata's
retired Azure AD B2C login and v1 data API: the integration had to
monkey-patch ``Client._get_tokens``, rebind ``API_URL`` at import time and
read a dozen private attributes, any of which could break without warning.

Implemented here instead: the Keycloak login and the single
``/consumer/metersforconsumer`` call Brunata's own readings page uses, which
carries each meter's reading together with its placement label.

The HTTP client is built and owned here, deliberately *not* taken from
``homeassistant.helpers.httpx_client`` — see ``async_create()``. Anything that
reads like an argument for dropping ``async_close()`` is out of date.
"""

from __future__ import annotations

import base64
import hashlib
import html
import logging
import math
import re
import secrets
import time
from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache, partial
from typing import Any, NamedTuple
from urllib.parse import parse_qs, urlparse

import httpx

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Brunata retired the v1 data API alongside the Keycloak migration: with a
# valid token, the v1 equivalent of this endpoint answers 401 while v2 works.
BASE_URL = "https://online.brunata.com"
API_URL = f"{BASE_URL}/online-webservice/v2/rest"

# Sent as the Referer on API calls — Brunata's own readings page, read off the
# live traffic in the browser's developer tools.
#
# Named REFERER_URL, not METERS_URL. It is *not* the meters endpoint, which is
# {API_URL}/consumer/metersforconsumer and is built in async_get_meters(). The
# old name was one of the three this codebase drew a conclusion from instead of
# reading, and it cost a production outage.
REFERER_URL = f"{BASE_URL}/react-online/meters-values"

# Where anything this integration could not handle is reported.
#
# There used to be two: this one, and a link straight to issue #39, which asked
# users to paste the numeric meterType of a meter that got skipped. That
# question is answered — see SUPPORTED_METER_TYPES — so both log lines point at
# the tracker itself. A meter still landing there is a fresh report, not an
# answer to the old one.
ISSUE_TRACKER_URL = "https://github.com/MSL-DA/brunata_online/issues"

# The only meter types this integration will surface. Everything else is
# dropped before it can become an entity.
#
# A safety boundary, not a feature limit. Brunata's portal can carry leak and
# smoke detectors — meterType 13 and 12 — and this integration polls hourly
# over a cloud API, up to an hour between an event and Home Assistant hearing
# about it. An entity invites an automation, and that automation would be
# dangerously slow. The only safe answer is not to create it.
#
# It fails closed: a meterType that is missing, unparseable or simply not
# listed here is skipped.
#
#   1 = heat cost allocator (radiator)
#   2 = water
#   5 = electricity
#
# How those numbers were established, because it matters: Brunata's own
# meterType table is returned by the locale resource, and a debug log from a
# live account printed it in full. In that table index 1 is "Radiator" and
# index 2 is "Water" — the two types whose meters we can check against real
# entities — so the same table's answer for 5 is a reading, not a guess. This
# is what issue #39 was waiting for.
#
# The same table gives 6 = "Energy", and it is deliberately *not* listed. A
# code being readable is not a reason to surface a meter type nobody has asked
# for: energy meters appear to be sold to housing associations rather than to
# private customers, so the first person to want one can say so and be the
# person it gets tested against. Adding it is one number and one line in the
# README. Leaving it out costs nothing until then.
#
# An electricity meter has since been through this code and resolved to kWh,
# which sensor.py's UNIT_MAP turns into UnitOfEnergy.KILO_WATT_HOUR with
# device class ENERGY. So 5 is not just a code read off a table any more; it
# is a type that has produced a working entity.
#
# The old reasoning is still wrong and worth keeping wrong: the presence of
# GJ/Gcal in UNIT_MAP never said anything about meterType. That is the
# measurementUnit table, a different table entirely. What changed is that the
# right table was finally read, not that the inference became acceptable.
#
# _log_unsupported_meter() names the code of anything dropped, so the list is
# extended by reading that line from a user's log, not by inference.
SUPPORTED_METER_TYPES = frozenset({1, 2, 5})

# Index 0 of Brunata's measurementUnit table is the literal string "undefined":
# Brunata saying it has not stated a unit, which is the same position as a code
# that does not resolve at all and is treated the same way. See _parse_meters().
UNDEFINED_UNIT = "undefined"


@lru_cache(maxsize=64)
def _log_unresolved_unit(meter_id: str, code: str) -> None:
    """Note a meter skipped for an unusable unit, once per process.

    Cached for the same reason as _log_unsupported_meter(): this runs per
    meter per poll, and the lookup tables do not change between them.

    The wording is deliberate. What the user sees is a sensor that stopped
    updating, so the line has to say the data is safe and nothing needs doing.
    """
    _LOGGER.warning(
        "Meter %s reports unit code %s, which cannot be resolved to a unit, so "
        "this meter is skipped for now. Its sensor stops updating and keeps "
        "the history it already has; it comes back on its own as soon as the "
        "unit resolves again. Creating it with the raw code as its unit would "
        "make Home Assistant treat it as a different measurement and discard "
        "that history, which cannot be undone. If this persists, please report "
        "it with this line at %s.",
        meter_id,
        code,
        ISSUE_TRACKER_URL,
    )


def _meter_type_code(raw: Any) -> int | None:
    """Coerce Brunata's meterType to an int, or None if it isn't one.

    Observed as an integer, but `unit` in the same payload is a string where it
    used to be an integer, so the same could happen here. None means "cannot be
    checked against the allowlist", which SUPPORTED_METER_TYPES treats as "do
    not surface".
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        try:
            return int(raw.strip())
        except ValueError:
            return None
    return None


@lru_cache(maxsize=64)
def _log_unsupported_meter(meter_id: str, code: str) -> None:
    """Note a skipped meter once per process, not once per poll.

    `code` is pre-formatted by the caller: lru_cache hashes its arguments, and
    a meterType arriving as a dict or list is unhashable. Passing it raw threw
    TypeError out of _parse_meters(), costing every meter that update — the
    exact failure this filter exists to avoid.
    """
    _LOGGER.info(
        "Meter %s has meterType %s, which this integration does not support, "
        "so no entity is created for it. Supported types are %s. If this "
        "meter measures consumption you expected to see in Home Assistant, "
        "please report it at %s with this line and what the meter physically "
        "measures — the log can give the code, but only you can say what the "
        "box on the wall is.",
        meter_id,
        code,
        sorted(SUPPORTED_METER_TYPES),
        ISSUE_TRACKER_URL,
    )


# Brunata's API is fronted by bot protection, so requests are made to look like
# the web app's. Fixed rather than randomised: brunata-api used fake_useragent,
# which added a dependency and a moving target for no benefit.
#
# Bumped by hand, and no test can tell when it is due — a stale version has no
# effect right up until the day the bot protection decides it does. Last set to
# Edge 151 (stable, July 2026). If logins start failing with no other
# explanation, look here first.
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36 Edg/151.0.0.0"
    ),
    "Sec-Ch-Ua": (
        '"Not/A)Brand";v="8", "Chromium";v="151", "Microsoft Edge";v="151"'
    ),
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Accept-Language": "en",
}

# The locale resource carries the lookup tables that turn the numeric codes in
# the meter payload into names and units.
LOCALE = "en"

KC_REALM_BASE = (
    "https://online.brunata.com/iam/realms/online-prod/protocol/openid-connect"
)
KC_AUTHORIZE_URL = f"{KC_REALM_BASE}/auth"
KC_TOKEN_URL = f"{KC_REALM_BASE}/token"
KC_CLIENT_ID = "82770188-c92e-4d16-927d-a15c472eda55"
KC_REDIRECT_URI = "https://online.brunata.com/auth-redirect"
KC_SCOPE = "openid offline_access"

# Keycloak renders the login form as <form id="kc-form-login" ... action="...">
_KC_FORM_ACTION_RE = re.compile(
    r'id="kc-form-login"[^>]*action="([^"]+)"', re.IGNORECASE
)
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)

# Renew slightly before the server's own expiry, so a slow request or a little
# clock drift doesn't send a token that expires mid-flight.
_EXPIRY_MARGIN_SECONDS = 30

REQUEST_TIMEOUT = 15.0


class BrunataError(Exception):
    """Base class for every error this client raises."""


class BrunataAuthError(BrunataError):
    """Credentials were rejected. The user has to re-authenticate."""


class BrunataConnectionError(BrunataError):
    """The server could not be reached. Worth retrying later."""


class BrunataApiError(BrunataError):
    """The server answered, but not with something usable.

    Carries the HTTP status when there was one, so a caller can tell 429 from
    503 from 404 without parsing the message; diagnostics.py reports it. None
    for failures with no status of their own — a login flow that changed
    shape, a locale resource without mappers.

    `retry_after` is set only for 429, from the header of the same name, and is
    how long Brunata asked us to wait. None when it did not say.
    """

    def __init__(
        self,
        message: str,
        status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class BrunataMeter:
    """A single meter and its most recent reading.

    Flat by design: every field comes straight from one entry in the
    /consumer/metersforconsumer payload, so an absent reading is simply
    ``value is None`` rather than a missing nested object.
    """

    meter_id: str
    meter_no: str | None
    # Resolved through the locale lookup table, e.g. "Radiator". A translation
    # Brunata owns, so it belongs in device names and nowhere a decision is
    # made — see meter_type_code.
    meter_type: str
    # The resolved unit name, e.g. "m3" or "units". Never a raw code and never
    # empty: _parse_meters() drops a meter whose unit it cannot name, because
    # an entity created with the wrong unit loses its history permanently and
    # a skipped one does not.
    unit: str
    # The raw meterType, already checked against SUPPORTED_METER_TYPES. This is
    # the stable half of the pair: sensor.py decides whether a meter is zeroed
    # every 1 January from this number, not from the name above, which would
    # silently stop matching if Brunata relabelled the entry.
    #
    # Defaults to None because that is what _meter_type_code() returns for a
    # value it cannot check, and every rule reading it must treat "unknown" the
    # way the allowlist does: as not one of ours.
    meter_type_code: int | None = None
    value: float | None = None
    reading_date: date | None = None
    # The customer's own label from Brunata's UI, e.g. "Koldt vand".
    placement: str | None = None
    # When Brunata installed the physical device. A change is a replacement
    # stated as fact, rather than inferred from a falling value.
    mounting_date: datetime | None = None
    # Digits Brunata itself displays: 3 for water, 0 for heat cost allocators.
    decimals: int | None = None
    # Whether the meter is currently sending readings.
    transmitting: bool | None = None


class ParseReport(NamedTuple):
    """What the last parse saw, beyond the meters it produced.

    The meters dictionary alone cannot answer "is this meter gone?". A meter
    can be absent from it for three reasons that mean different things:

    * Brunata dismounted it — it really is gone.
    * Its meterType is not one we surface — it never had an entity.
    * Its unit could not be resolved this poll — it is still on the wall, and
      we simply could not name what it measures.

    The third case must not be mistaken for the first, and neither must "the
    payload was empty, so we learned nothing at all". This record carries the
    two facts needed to tell them apart.
    """

    # Meter ids skipped because their unit code did not resolve. Absent from
    # the meters dictionary, but still reported by Brunata.
    unresolved_unit_meter_ids: frozenset[str]
    # How many entries the payload held before any filtering. Zero means
    # Brunata reported nothing, which is not evidence about any single meter.
    raw_item_count: int


class ParsedMeters(NamedTuple):
    """What _parse_meters() returns: the meters, and what it saw getting them."""

    meters: dict[str, BrunataMeter]
    report: ParseReport


def _as_text(raw: Any) -> str:
    """Coerce a lookup code to text.

    The codes have been observed as both integers and strings, so they are
    normalised before being parsed, and an absent code becomes "" rather than
    the string "None". There is no branch for a code that is already a string:
    str() returns one unchanged.
    """
    if raw is None:
        return ""
    return str(raw)


@lru_cache(maxsize=64)
def _warn_unresolved_code(what: str, code: str, table_size: int) -> None:
    """Warn about an unresolved lookup code, once per process.

    _resolve() runs per meter per poll, so without the cache an unresolvable
    code means 24 identical lines a day. The tables are static and
    account-independent, so the second warning carries nothing the first did.

    It says only that the code did not resolve. What happens next differs by
    field — the meter type falls back, the unit skips the meter — so the
    callers say that part.
    """
    _LOGGER.warning(
        "Brunata %s code %r does not resolve in the lookup table (%s entries).",
        what,
        code,
        table_size,
    )


def _resolve(table: list[str], raw: Any, what: str) -> str | None:
    """Resolve a numeric code against one of the locale lookup tables.

    The payload carries indices, not names: meterType 2 means whatever sits at
    index 2 of the meterType table. Returns None when the table cannot answer
    and leaves it to the caller to decide what that is worth — the two fields
    this serves do not deserve the same answer. See _lookup() and
    _parse_meters().
    """
    code = _as_text(raw)
    if not code:
        return None

    try:
        index = int(code)
        # Python indexes lists from both ends and Brunata does not. Without
        # this, -1 returns the table's *last* entry rather than raising: a
        # wrong but perfectly valid unit, with the wrong device class, the
        # wrong state class, and statistics that look right and are not.
        if index < 0:
            raise IndexError(index)
        name = table[index]
    except (ValueError, IndexError):
        name = None

    # The live tables contain null entries — 7 of 28 meter types and 34 of 96
    # units are reserved slots Brunata has not filled in. An index landing on
    # one means the same as an index past the end.
    if not isinstance(name, str) or not name.strip():
        _warn_unresolved_code(what, code, len(table))
        return None

    # Some live entries carry a trailing space, which would end up in the
    # device name.
    return name.strip()


def _lookup(table: list[str], raw: Any, what: str) -> str:
    """Resolve a code, falling back to the raw code when the table cannot.

    Used for the meter type only, where the fallback is deliberate: the name
    reaches the device name and the `model` field and nothing else, since every
    decision hangs on meter_type_code. A device called "2" is ugly, visible and
    fixes itself when the table resolves again.

    The unit gets no fallback — see _parse_meters(). That asymmetry is the
    point: one field is cosmetic when it goes wrong, the other costs the meter
    its history.
    """
    return _resolve(table, raw, what) or _as_text(raw)


def parse_timestamp(raw: Any) -> datetime | None:
    """Parse one of Brunata's ISO timestamps, e.g. mountingDate.

    They carry an offset ("2018-10-23T14:09:22+02:00"), so the result is
    timezone-aware. Unparseable becomes None rather than raising: a meter with
    an odd date is still a meter.

    Public rather than underscored because sensor.py calls it. The restore
    store holds what this module serialised, so there is one spelling of a
    Brunata date and one place that knows it.
    """
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            _LOGGER.debug("Could not parse timestamp %r", raw)
    return None


def _expires_in_seconds(raw: Any) -> float | None:
    """Read a token lifetime, or None when it cannot be read.

    _store_tokens() used to call float() on this directly. A string that is not
    a number raised ValueError and a list raised TypeError, and both callers
    catch only BrunataApiError around that call — so either one went straight
    out of _async_login(), out of async_get_meters() and into the coordinator
    as an unexpected exception rather than as a clean retry or reauth.

    None means "no usable expiry", which _store_tokens() already treats as an
    immediately unusable token: the next request logs in again. Failing that
    way costs one login; failing the other way costs the update.

    Bools are refused for the same reason _meter_type_code() refuses them.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        _LOGGER.debug("Brunata sent an unreadable expires_in: %r", raw)
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        _LOGGER.debug("Brunata sent an unreadable expires_in: %r", raw)
        return None
    if not math.isfinite(seconds):
        _LOGGER.debug("Brunata sent a non-finite expires_in: %r", raw)
        return None
    return seconds


def _parse_value(raw: Any) -> float | None:
    """Parse a meter reading, tolerating anything that is not a number.

    Every other field here degrades to None rather than raising, so one odd row
    costs one meter. A bare float() would break that: the ValueError escapes
    _parse_meters() untranslated and reaches the coordinator as an unexpected
    exception, costing every meter that update.

    float() is not the whole guard, though. It also accepts "NaN", "Infinity"
    and "1e400", and none of those is a meter reading. nan is the worse of the
    two: every comparison against it is false, so _accept_reading() takes it
    whenever there is no previous value — first poll after setup, or after a
    restart with nothing to restore — and it becomes the sensor's state.
    """
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        _LOGGER.debug("Could not parse reading value %r", raw)
        return None
    if not math.isfinite(value):
        _LOGGER.debug("Ignoring non-finite reading value %r", raw)
        return None
    return value


def parse_reading_date(raw: Any) -> date | None:
    """Parse Brunata's reading date, tolerating a full timestamp.

    Public for the same reason as parse_timestamp(): sensor._as_date() hands it
    the string it read back out of the restore store.
    """
    if isinstance(raw, str):
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            _LOGGER.debug("Could not parse reading date %r", raw)
    return None


def format_date(value: Any) -> Any:
    """Render a date or datetime as an ISO string, leaving anything else alone.

    The counterpart to the two parsers above, and public for the same reason:
    sensor.py needs it for the reading_date attribute and diagnostics.py for
    the downloaded report. It lived in both of those files as a private copy
    with the same body and the same docstring note — the 1.3.1 round put date
    *parsing* in one place on the argument that there should be one spelling of
    a Brunata date and one module that knows it, and this is the other half of
    that argument.

    Passing non-dates through unchanged is what both callers want. A reading
    date restored from the state machine is already the ISO string we wrote,
    and every other field in a diagnostics report is already JSON.

    ``date`` alone covers both cases: datetime subclasses it, and both spell
    isoformat().
    """
    if isinstance(value, date):
        return value.isoformat()
    return value


class BrunataApiClient:
    """Talks to Brunata Online on behalf of one account."""

    def __init__(self, email: str, password: str, http_client: httpx.AsyncClient) -> None:
        """Use async_create() instead; it builds the HTTP client correctly."""
        self._email = email
        self._password = password
        self._client = http_client

        self._meter_types: list[str] = []
        self._measurement_units: list[str] = []
        # Whether the locale resource has been fetched, which is not the same
        # as the tables being non-empty. See _async_ensure_lookup_tables().
        self._lookup_tables_loaded = False

        # What the most recent parse saw. See last_parse_report().
        self._last_parse_report = ParseReport(frozenset(), 0)

        self._access_token: str | None = None
        self._token_type: str = "Bearer"
        self._refresh_token: str | None = None
        self._expires_at: float = 0.0

    @classmethod
    async def async_create(
        cls, hass: HomeAssistant, email: str, password: str
    ) -> BrunataApiClient:
        """Build a client with its own cookie jar and connection pool.

        Deliberately not homeassistant.helpers.httpx_client: that returns a
        client Home Assistant owns and closes at shutdown, and it warns when an
        integration closes it. We hold Keycloak session cookies and a bearer
        token and want them gone the moment the entry unloads, so we own it.

        Built in the executor because httpx.AsyncClient loads the certificate
        store from disk when it builds its SSL context.
        """
        http_client = await hass.async_add_executor_job(
            partial(
                httpx.AsyncClient,
                timeout=httpx.Timeout(REQUEST_TIMEOUT),
                headers=DEFAULT_HEADERS,
            )
        )
        return cls(email, password, http_client)

    def diagnostics(self) -> dict[str, Any]:
        """Return the client's internal state for the diagnostics download.

        A method rather than five attribute reads from diagnostics.py: what is
        safe to publish belongs with the fields. Vendoring this client removed
        a dozen reads of a third party's private attributes, and there is no
        reason to reintroduce the pattern against ourselves.

        Neither token is included, only whether one exists. An access token is
        a working credential, and diagnostics files get attached to public
        issues.
        """
        return {
            "lookup_tables_loaded": self._lookup_tables_loaded,
            # Copied, not handed out: the caller serialises these into a file
            # and must not be able to mutate what the parser looks up in.
            "meter_types": list(self._meter_types),
            "measurement_units": list(self._measurement_units),
            "has_access_token": self._access_token is not None,
            "has_refresh_token": self._refresh_token is not None,
        }

    def last_parse_report(self) -> ParseReport:
        """Return what the most recent async_get_meters() call saw.

        A method rather than a bare attribute for the same reason as
        diagnostics(): what the rest of the integration may read off this
        client is decided here, next to the field.

        The coordinator is the only caller and its updates are serialised, so
        there is nothing to race. Callers must read it immediately after the
        call it belongs to.

        See ParseReport for why the meters dictionary alone is not enough.
        """
        return self._last_parse_report

    async def async_close(self) -> None:
        """Close the underlying HTTP client.

        Called on unload. Without it every reload — including the automatic one
        after a reauth — leaks keep-alive sockets for the life of the process.
        """
        await self._client.aclose()

    # --- Public API ---------------------------------------------------------

    async def async_validate_credentials(self) -> None:
        """Log in once, to check the credentials during config flow.

        Raises BrunataAuthError if they are wrong, BrunataConnectionError if
        Brunata could not be reached.
        """
        await self._async_login(force=True)

    async def async_get_meters(self) -> dict[str, BrunataMeter]:
        """Return every mounted meter, keyed by meter ID.

        One call to /consumer/metersforconsumer, which is what Brunata's own
        readings page uses: meter, latest reading, placement, mounting and
        dismounting dates and display precision in a single flat list.
        """
        await self._async_ensure_lookup_tables()

        response = await self._async_authenticated_get(
            f"{API_URL}/consumer/metersforconsumer"
        )

        parsed = _parse_meters(
            _payload(response),
            meter_types=self._meter_types,
            measurement_units=self._measurement_units,
        )
        self._last_parse_report = parsed.report
        return parsed.meters

    async def _async_ensure_lookup_tables(self) -> None:
        """Fetch the locale resource once per client, or fail the update.

        The meter payload identifies type and unit by index into these tables.
        Without them every meter is united by a bare number — "8" where the
        user had "m³" — and Home Assistant treats a changed unit as a new
        series, discarding the statistics behind the old one. That cannot be
        undone.

        So an unusable response is an error, not a warning: failing costs one
        poll, and the sensors keep their values until the next hour. An empty
        table counts as unusable too — both mean no lookups are possible, and
        the shape of the failure is Brunata's business.

        The caching guard is a separate flag rather than "are the tables
        non-empty", and it is only set on the success path. Its job is to stop
        a *loaded* table being re-fetched; leaving it unset when nothing loaded
        is a retry, not that loop.
        """
        if self._lookup_tables_loaded:
            return

        response = await self._async_authenticated_get(
            f"{API_URL}/locales/{LOCALE}/common"
        )
        payload = _payload(response)
        # Guarded rather than assumed: a payload that is a list (or anything
        # else) would otherwise raise AttributeError here instead of the
        # BrunataApiError the coordinator knows how to translate.
        mappers = payload.get("mappers") if isinstance(payload, dict) else None
        if not isinstance(mappers, dict):
            raise BrunataApiError("Brunata locale resource carried no mappers")

        meter_types = list(mappers.get("meterType") or [])
        measurement_units = list(mappers.get("measurementUnit") or [])

        if not meter_types or not measurement_units:
            raise BrunataApiError(
                f"Brunata locale resource carried {len(meter_types)} meter "
                f"types and {len(measurement_units)} units, so meter types "
                "and units cannot be resolved"
            )

        self._meter_types = meter_types
        self._measurement_units = measurement_units
        self._lookup_tables_loaded = True

        # Logged in full, not just counted. These are Brunata's own translation
        # tables — static, identical for every account, free of personal data —
        # and the only authoritative answer to which meter types and units the
        # service can express at all. Any one account uses a handful.
        _LOGGER.debug(
            "Loaded Brunata lookup tables (%s meter types, %s units). "
            "meterType=%s measurementUnit=%s",
            len(self._meter_types),
            len(self._measurement_units),
            self._meter_types,
            self._measurement_units,
        )

    # --- HTTP ---------------------------------------------------------------

    async def _async_authenticated_get(self, url: str) -> httpx.Response:
        """GET an API endpoint, retrying once with a brand-new login on 401/403.

        A cached token can look locally valid while the server no longer
        accepts it — a revoked Keycloak session, or clock drift. Rather than
        declaring the credentials wrong, discard the token and try exactly one
        fresh login. Only a 401 on *that* attempt means the credentials are no
        longer accepted.

        Shared by both endpoints on purpose. The retry used to sit in the
        meters call alone, so a stale token on the locale resource surfaced as
        "invalid JSON" — and since the tables are fetched once per client, that
        repeated on every poll until something else happened to fix it.
        """
        response = await self._async_get(url, force_login=False)
        if response.status_code not in (401, 403):
            return response

        _LOGGER.warning(
            "Brunata returned %s with a cached token — retrying once with a "
            "fresh login",
            response.status_code,
        )
        response = await self._async_get(url, force_login=True)

        if response.status_code in (401, 403):
            raise BrunataAuthError(
                f"Brunata returned {response.status_code} even after a fresh "
                "login. Check credentials and account access."
            )
        return response

    async def _async_get(self, url: str, *, force_login: bool) -> httpx.Response:
        """Log in if needed, then GET the URL with the API's expected headers."""
        await self._async_login(force=force_login)
        return await self._async_request(
            "GET",
            url,
            headers={
                "Authorization": f"{self._token_type} {self._access_token}",
                "Referer": REFERER_URL,
            },
        )

    async def _async_request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Perform a request, mapping transport failures to our own error.

        httpx.RequestError, not httpx.TransportError. Read off httpx's own
        exception hierarchy rather than inferred from the names:

            RequestError
              + TransportError
                - TimeoutException, NetworkError, ProtocolError,
                  ProxyError, UnsupportedProtocol
              + DecodingError
              + TooManyRedirects
            HTTPStatusError
            InvalidURL          <- outside RequestError

        RequestError adds exactly two cases over TransportError, and both mean
        the same thing as the rest: the request produced no usable response.
        TooManyRedirects can only reach us from _async_authorize(), the one
        call made with follow_redirects=True, and DecodingError from a broken
        content-encoding somewhere in the path. Caught as TransportError only,
        both escaped this module untranslated and reached the coordinator as
        unexpected exceptions with a traceback, instead of as the clean retry
        that keeps the last known values.

        Not httpx.HTTPError: InvalidURL sits outside RequestError, so a URL
        this integration built wrong keeps failing loudly instead of being
        disguised as a network problem.
        """
        try:
            return await self._client.request(method, url, **kwargs)
        except httpx.RequestError as err:
            raise BrunataConnectionError(f"Cannot reach Brunata: {err}") from err

    # --- Authentication -----------------------------------------------------

    @property
    def _token_is_usable(self) -> bool:
        return bool(self._access_token) and time.time() < self._expires_at

    def _store_tokens(self, payload: dict[str, Any]) -> None:
        """Record a token response.

        Every field is replaced rather than merged. Merging let an old expiry
        survive a response that carried none, so an expired token could be
        reported as usable and the login it needed was skipped.
        """
        access_token = payload.get("access_token")
        if not access_token:
            raise BrunataApiError("Brunata returned no access token")

        self._access_token = access_token
        self._token_type = payload.get("token_type") or "Bearer"
        self._refresh_token = payload.get("refresh_token")

        expires_in = _expires_in_seconds(payload.get("expires_in"))
        self._expires_at = (
            time.time() + expires_in - _EXPIRY_MARGIN_SECONDS
            if expires_in is not None
            else 0.0
        )

    def _clear_tokens(self) -> None:
        self._access_token = None
        self._refresh_token = None
        self._expires_at = 0.0

    async def _async_login(self, *, force: bool = False) -> None:
        """Ensure a usable access token, doing as little work as possible."""
        if not force:
            if self._token_is_usable:
                return
            if await self._async_try_refresh():
                return
        else:
            # The server rejected a token we believed was good. Keycloak's SSO
            # cookies live on this same client, so keeping them would just mint
            # another token from the very session that was refused — and the
            # refresh token from that session is no better.
            self._clear_tokens()
            self._client.cookies.clear()

        await self._async_browser_login()

    async def _async_try_refresh(self) -> bool:
        """Redeem the refresh token. Returns False to fall back to a login.

        We request the offline_access scope, so Keycloak issues one. Using it
        turns a renewal into a single POST instead of a three-request browser
        login, which is faster and far less likely to trip bot protection.
        """
        if not self._refresh_token:
            return False

        response = await self._async_request(
            "POST",
            KC_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": KC_CLIENT_ID,
                "refresh_token": self._refresh_token,
            },
            follow_redirects=False,
        )
        if response.status_code != 200:
            _LOGGER.debug(
                "Brunata refresh token rejected (%s) — falling back to a full login",
                response.status_code,
            )
            return False

        try:
            self._store_tokens(_payload(response))
        except BrunataApiError:
            return False

        _LOGGER.debug("Brunata access token renewed via refresh token")
        return True

    async def _async_browser_login(self) -> None:
        """Run the OAuth 2.0 Authorization Code + PKCE flow by hand."""
        # secrets.token_urlsafe uses only the unreserved characters RFC 7636
        # allows, and always lands inside its 43-128 character range.
        code_verifier = secrets.token_urlsafe(64)
        code_challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )

        auth_code = await self._async_authorize(code_challenge)
        response = await self._async_request(
            "POST",
            KC_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "client_id": KC_CLIENT_ID,
                "redirect_uri": KC_REDIRECT_URI,
                "code": auth_code,
                "code_verifier": code_verifier,
            },
            follow_redirects=False,
        )
        if response.status_code != 200:
            self._clear_tokens()
            raise BrunataAuthError(
                f"Brunata rejected the authorization code ({response.status_code})"
            )

        try:
            self._store_tokens(_payload(response))
        except BrunataApiError as err:
            self._clear_tokens()
            raise BrunataAuthError(str(err)) from err

    async def _async_authorize(self, code_challenge: str) -> str:
        """Obtain an authorization code, logging in if we have to."""
        page = await self._async_request(
            "GET",
            KC_AUTHORIZE_URL,
            params={
                "client_id": KC_CLIENT_ID,
                "redirect_uri": KC_REDIRECT_URI,
                "scope": KC_SCOPE,
                "response_type": "code",
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            },
            follow_redirects=True,
        )
        if page.status_code >= 400:
            raise BrunataApiError(
                f"Brunata authorize endpoint returned {page.status_code}",
                page.status_code,
            )

        # A live Keycloak SSO session redirects straight to the redirect URI
        # with ?code=... and renders no form. That is success: treating the
        # missing form as an error would prompt for credentials that work.
        if auth_code := _code_from_url(page.url):
            _LOGGER.debug("Brunata SSO session still active — no login form needed")
            return auth_code

        # Not a BrunataAuthError. Nothing here says the credentials are wrong;
        # it says Keycloak rendered something this code no longer recognises.
        # An auth error would open reauth and ask for a password that is
        # perfectly valid, and the next attempt would fail in the same place.
        # An API error becomes UpdateFailed, so HA retries and the log says
        # what actually broke.
        match = _KC_FORM_ACTION_RE.search(page.text)
        if not match:
            raise BrunataApiError(
                "Brunata login form not found — the login flow has changed"
            )

        auth = await self._async_request(
            "POST",
            html.unescape(match.group(1)),
            data={
                "username": self._email,
                "password": self._password,
                "credentialId": "",
            },
            follow_redirects=False,
        )
        # On success Keycloak issues a redirect; on failure it re-renders the
        # form with an error message.
        if auth.status_code not in _REDIRECT_STATUSES:
            raise BrunataAuthError(
                "Brunata rejected the login — check the email and password"
            )

        # Both mean the credentials were accepted — Keycloak issued a redirect
        # — and that what came back is not the shape this code expects. Same
        # reasoning as the missing form above: an API error, not a prompt for
        # credentials that already worked.
        location = auth.headers.get("Location", "")
        if not location.startswith(KC_REDIRECT_URI):
            raise BrunataApiError(f"Unexpected redirect after login: {location}")

        auth_code = _code_from_url(location)
        if not auth_code:
            raise BrunataApiError("Brunata returned no authorization code")

        # Logged because the alternative routes each say so — the refresh
        # ("access token renewed via refresh token") and the SSO shortcut
        # ("SSO session still active"). Without this line a full login was the
        # only path that produced no output at all, so a log could not tell
        # "the refresh token is working" from "we log in from scratch every
        # hour" — which is the difference between two requests per poll and
        # four, and the one that matters to a bot-protected endpoint.
        _LOGGER.debug("Brunata login form accepted the credentials")
        return auth_code


def _code_from_url(url: Any) -> str | None:
    """Pull the OAuth authorization code out of a redirect URL."""
    return parse_qs(urlparse(str(url)).query).get("code", [None])[0]


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Read the Retry-After header, in its delay-seconds form.

    RFC 9110 also allows an HTTP-date. That form is not read here: it needs the
    server's clock to agree with ours to be worth anything, and getting it
    wrong means waiting either far too long or not at all. Falling back to a
    fixed default is the safer failure, and the coordinator does that.

    The finiteness check is not decoration. float() accepts "inf", "Infinity"
    and "1e400", and inf is greater than zero, so such a header used to pass
    straight through to timedelta(seconds=...) in the coordinator — which
    raises OverflowError from inside the very except block that exists to
    translate this error, so it escaped as an unexpected exception. A header
    that is not a real number is no header at all, same as the date form.

    A number that is merely enormous is left to the caller: the coordinator
    caps it, because how long we are willing to stay away is its decision, not
    the parser's.
    """
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        seconds = float(str(raw).strip())
    except ValueError:
        _LOGGER.debug("Brunata sent an unreadable Retry-After: %r", raw)
        return None
    if not math.isfinite(seconds):
        _LOGGER.debug("Brunata sent a non-finite Retry-After: %r", raw)
        return None
    return seconds if seconds > 0 else None


def _payload(response: httpx.Response) -> Any:
    """Decode a JSON response, raising on anything that isn't usable."""
    status = response.status_code

    if status == 429:
        raise BrunataApiError(
            f"Brunata rate limit reached ({status})",
            status,
            _retry_after_seconds(response),
        )
    if status >= 500:
        raise BrunataApiError(f"Brunata server error ({status})", status)
    if status == 404:
        raise BrunataApiError(f"Brunata endpoint not found ({status})", status)

    try:
        payload = response.json()
    except ValueError as err:
        raise BrunataApiError(f"Brunata returned invalid JSON: {err}", status) from err

    # Brunata reports some errors with HTTP 200 and an error body.
    if isinstance(payload, dict) and (
        payload.get("errorCode") is not None or payload.get("errorMessage") is not None
    ):
        code = payload.get("errorCode")
        message = payload.get("errorMessage")
        if code == "WB_WEBSERVICES_0011" or (
            isinstance(message, str) and "Not authorized" in message
        ):
            raise BrunataAuthError(f"Brunata authentication failed: {message}")
        raise BrunataApiError(f"Brunata returned error {code}: {message}", status)

    return payload


def _parse_meters(
    payload: Any,
    *,
    meter_types: list[str] | None = None,
    measurement_units: list[str] | None = None,
) -> ParsedMeters:
    """Turn a /consumer/metersforconsumer payload into meters keyed by ID.

    Each entry is flat — meter, latest reading and metadata side by side — so
    there is no wrapper object to guard against.

    Returns the meters *and* a ParseReport. The report exists because an
    absent meter is ambiguous: dismounted, unsupported, or merely skipped for
    a unit we could not name this poll. Only the first of those means the
    meter is gone, and the rest of the integration cannot tell without being
    told. See ParseReport.
    """
    if not isinstance(payload, list):
        raise BrunataApiError(
            f"Expected a list of meters, got {type(payload).__name__}"
        )

    _LOGGER.debug("Brunata returned %s raw item(s)", len(payload))

    meters: dict[str, BrunataMeter] = {}
    unresolved_units: set[str] = set()
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            _LOGGER.debug(
                "Item %s skipped: not an object (%s)", index, type(item).__name__
            )
            continue

        raw_id = item.get("meterId")
        if raw_id is None:
            _LOGGER.debug("Item %s skipped: meterId is null", index)
            continue

        # A dismounted meter is one Brunata has physically removed; its final
        # reading never changes again. Dropping it makes the entity go
        # unavailable, which is what a removed meter should look like.
        #
        # Checked before the allowlist. Both drop the item, so the order cannot
        # change which meters reach Home Assistant — but the allowlist writes a
        # log line asking the user to report the meter's type, and a meter
        # Brunata has taken off the wall is not one anybody needs identified.
        dismounted = parse_timestamp(item.get("dismountedDate"))
        if dismounted is not None:
            _LOGGER.debug(
                "Item %s (meter %s) skipped: dismounted on %s",
                index,
                raw_id,
                dismounted.date(),
            )
            continue

        # Before anything else is read: is this a kind of meter we are willing
        # to surface at all? See SUPPORTED_METER_TYPES.
        type_code = _meter_type_code(item.get("meterType"))
        if type_code not in SUPPORTED_METER_TYPES:
            _log_unsupported_meter(str(raw_id), repr(item.get("meterType")))
            continue

        # And is its unit something we can name? A code that does not resolve,
        # one Brunata left as "undefined", or none at all all say the same
        # thing: nobody knows what this meter measures in.
        #
        # Skipped rather than filled in. Home Assistant treats a changed unit
        # on an existing sensor as a different measurement and discards the
        # statistics behind it, permanently — so "8" in place of "m³" is a
        # permanent loss, while skipping is a pause the meter recovers from on
        # the next poll that resolves. Same rule as the allowlist above,
        # applied to the field where getting it wrong is the more expensive of
        # the two. The meter type keeps its fallback; see _lookup().
        unit = _resolve(
            measurement_units or [], item.get("unit"), "measurement unit"
        )
        if unit is None or unit.lower() == UNDEFINED_UNIT:
            _log_unresolved_unit(str(raw_id), repr(item.get("unit")))
            # Recorded, not just dropped. Brunata still reports this meter, so
            # its sensor must keep its value and stay available, and its
            # device must not become removable — see BrunataSensor.available
            # and async_remove_config_entry_device().
            unresolved_units.add(str(raw_id))
            continue

        value = _parse_value(item.get("latestReadingValue"))
        decimals = item.get("decimals")
        placement = item.get("placement")
        transmitting = item.get("transmitting")

        _LOGGER.debug(
            "Item %s: meterId=%r meterType=%r unit=%r decimals=%r "
            "transmitting=%r has reading=%s",
            index,
            raw_id,
            item.get("meterType"),
            item.get("unit"),
            decimals,
            transmitting,
            value is not None,
        )

        meters[str(raw_id)] = BrunataMeter(
            meter_id=str(raw_id),
            meter_no=item.get("meterNo"),
            meter_type=_lookup(meter_types or [], item.get("meterType"), "meter type"),
            # The code the allowlist check above was made on, not a second
            # reading of the field: the two must never be able to disagree.
            meter_type_code=type_code,
            unit=unit,
            value=value,
            reading_date=parse_reading_date(item.get("latestReadingDate")),
            placement=placement if isinstance(placement, str) and placement else None,
            mounting_date=parse_timestamp(item.get("mountingDate")),
            # Three things have to hold before this becomes a display
            # precision. It must be an int, because Home Assistant counts
            # digits with it. It must not be a bool, because bool subclasses
            # int and `true` would otherwise become a precision of 1 —
            # _meter_type_code() guards the same way, and the two fields read
            # the same kind of input. And it must not be negative: there is no
            # such thing as minus four decimal places, and passing one on would
            # put a number Brunata never reported into the entity.
            #
            # Anything else is dropped, and sensor.py falls back to a precision
            # chosen from the unit.
            decimals=(
                int(decimals)
                if isinstance(decimals, int)
                and not isinstance(decimals, bool)
                and decimals >= 0
                else None
            ),
            transmitting=transmitting if isinstance(transmitting, bool) else None,
        )

    _LOGGER.debug("Parsed %s meters from Brunata", len(meters))
    return ParsedMeters(
        meters, ParseReport(frozenset(unresolved_units), len(payload))
    )

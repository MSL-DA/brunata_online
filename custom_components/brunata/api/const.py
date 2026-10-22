"""Addresses, request headers and fixed values for the Brunata API.

Nothing here imports from the rest of the package, which is what makes it safe
for every other module to import from: the dependency arrows all point this
way and none point back.
"""

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
# Both log lines that name it point at the tracker itself rather than at any
# one issue: a meter landing there is a fresh report, not an answer to an old
# question.
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
#   6 = energy (heat meter)
#
# How those numbers are known, because it matters: Brunata's own meterType
# table is returned by the locale resource, and a debug log from a live account
# printed it in full. In that table index 1 is "Radiator" and index 2 is
# "Water" — the two types whose meters can be checked against real entities —
# so the same table's answer for 5 is a reading, not a guess. An electricity
# meter has been through this code and resolved to kWh, which sensor.py's
# UNIT_MAP turns into UnitOfEnergy.KILO_WATT_HOUR with device class ENERGY, so
# 5 is a type that has produced a working entity.
#
# The one inference that must never be made here: UNIT_MAP says nothing about
# meterType. GJ and Gcal being in it is a fact about the measurementUnit table,
# which is a different table entirely. A meter type is added by reading the
# meterType table, not by reasoning from a unit.
#
# The same table gives 6 = "Energy". It was left out until somebody had such a
# meter, and issue #93 is that somebody: the user reports a missing heating
# meter, and the log dropped exactly one meter that was still mounted, with
# meterType 6. Nothing here was reasoned from a unit.
#
# What issue #93 does not show is the unit, because the allowlist drops a meter
# before its unit is read. No special handling is needed for that: sensor.py
# decides device class and state class from the resolved unit alone, so an
# energy unit in UNIT_MAP gives an ENERGY sensor that only counts up, and
# anything else gets the same cautious default as any unrecognised unit. A
# heat meter is zeroed only when it is replaced, which _accept_reading()
# already recognises; it is not in ANNUAL_RESET_METER_TYPES.
#
# _log_unsupported_meter() names the code of anything dropped, so the list is
# extended by reading that line from a user's log, not by inference.
SUPPORTED_METER_TYPES = frozenset({1, 2, 5, 6})

# Index 0 of Brunata's measurementUnit table is the literal string "undefined":
# Brunata saying it has not stated a unit, which is the same position as a code
# that does not resolve at all and is treated the same way. See _parse_meters().
UNDEFINED_UNIT = "undefined"

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

REQUEST_TIMEOUT = 15.0

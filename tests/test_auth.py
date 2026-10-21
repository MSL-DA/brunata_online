"""Tests for BrunataAuth, the Keycloak login.

This is the most fragile code in the integration: it drives a browser login
flow by hand, depends on the shape of Keycloak's HTML and redirects, and runs
against production on every single update. It previously had no coverage at
all, so the two regressions it has already had — the SSO redirect being
mistaken for a failure, and stale token expiry surviving a refresh — were both
found in the field rather than in CI.

The fake session below deliberately mimics only what the code actually touches
on ``httpx.AsyncClient``: headers, cookies, get() and post().
"""

import time

import pytest

from custom_components.brunata.api import (
    KC_AUTHORIZE_URL,
    KC_CLIENT_ID,
    KC_REDIRECT_URI,
    KC_TOKEN_URL,
    BrunataApiError,
    BrunataAuthError,
)
from custom_components.brunata.api.auth import BrunataAuth

LOGIN_FORM_HTML = (
    '<html><body><form id="kc-form-login" onsubmit="return true;" '
    'action="https://online.brunata.com/iam/login-actions/authenticate?'
    'session_code=abc&amp;execution=def" method="post">'
    '<input name="username"><input name="password"></form></body></html>'
)

# What Brunata's token endpoint reports as the access token's lifetime. Measured
# rather than assumed: a debug log from 30 August 2026 shows the client renewing
# via the refresh token on every second hourly poll and skipping it on the
# others, which puts the real lifetime between 46 and 104 minutes.
TOKEN_LIFETIME_SECONDS = 3600

TOKEN_RESPONSE = {
    "access_token": "new-access-token",
    "refresh_token": "new-refresh-token",
    "token_type": "Bearer",
    "expires_in": TOKEN_LIFETIME_SECONDS,
}


class FakeResponse:
    """Minimal stand-in for an httpx.Response."""

    def __init__(self, *, url="", text="", status_code=200, headers=None, json_data=None):
        self.url = url
        self.text = text
        self.status_code = status_code
        self.headers = headers or {}
        self._json_data = json_data

    def json(self):
        return self._json_data


class FakeCookies:
    def __init__(self):
        self.cleared = False

    def clear(self):
        self.cleared = True


class FakeHttpClient:
    """Stand-in for the httpx.AsyncClient the API layer holds."""

    def __init__(self, *, authorize=None, form_post=None, token_posts=None):
        self.cookies = FakeCookies()
        self.requests = []
        self.closed = False
        self._authorize = authorize
        self._form_post = form_post
        self._token_posts = list(token_posts or [])

    async def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs.get("data")))
        if method == "GET":
            assert self._authorize is not None, "unexpected authorize request"
            return self._authorize
        if url == KC_TOKEN_URL:
            assert self._token_posts, "unexpected token request"
            return self._token_posts.pop(0)
        assert self._form_post is not None, "unexpected credential POST"
        return self._form_post

    async def aclose(self):
        self.closed = True

    def request_methods(self):
        return [
            f"{m} {'token' if url == KC_TOKEN_URL else 'other'}"
            for m, url, _ in self.requests
        ]


def make_auth(http, *, access_token=None, refresh_token=None, expires_at=0.0):
    """Build a BrunataAuth wired to a fake HTTP client."""
    auth = BrunataAuth("user@example.com", "s3cret", http)
    auth._access_token = access_token
    auth._token_type = "Bearer"
    auth._refresh_token = refresh_token
    auth._expires_at = expires_at
    return auth


def _authorize_with_form():
    return FakeResponse(url=f"{KC_AUTHORIZE_URL}?client_id={KC_CLIENT_ID}", text=LOGIN_FORM_HTML)


def _authorize_with_sso_redirect(code="sso-code"):
    """Keycloak short-circuits straight to the redirect URI when SSO is live."""
    return FakeResponse(url=f"{KC_REDIRECT_URI}?code={code}&state=xyz", text="")


def _successful_form_post(code="form-code"):
    return FakeResponse(
        status_code=302, headers={"Location": f"{KC_REDIRECT_URI}?code={code}"}
    )


def _token_ok(payload=None):
    return FakeResponse(json_data=payload or dict(TOKEN_RESPONSE))


async def test_usable_token_skips_the_network_entirely():
    """async_ensure_token() is called before every request; without the reuse
    check each one would trigger a full browser login."""
    http = FakeHttpClient()
    auth = make_auth(http, access_token="still-good", expires_at=time.time() + 300)

    await auth.async_ensure_token()
    assert http.requests == []


async def test_the_authorization_header_carries_the_token_type_keycloak_sent():
    """The header pairs the token with its type, and the type is not a constant.

    _store_tokens() reads token_type off the response and falls back to
    "Bearer" only when it is absent, so that is the one place the name
    belongs. Writing it into the header instead would send "Bearer" on a token
    Keycloak had typed differently, every request would come back 401, and
    nothing in the log would say why.

    Nothing held that in place: hardcoding "Bearer" in async_authorization()
    left every other test in this file and in test_api.py green.
    """
    http = FakeHttpClient()
    auth = make_auth(http, access_token="still-good", expires_at=time.time() + 300)
    auth._token_type = "DPoP"

    assert await auth.async_authorization() == "DPoP still-good"
    # The token was usable, so the header cost no request.
    assert http.requests == []


async def test_expired_token_is_not_reused():
    http = FakeHttpClient(token_posts=[_token_ok()])
    auth = make_auth(
        http, access_token="expired", refresh_token="r", expires_at=time.time() - 1
    )

    await auth.async_ensure_token()
    assert http.request_methods() == ["POST token"]


async def test_refresh_token_avoids_the_browser_flow():
    """An expired access token with a live refresh token costs one POST, not a
    three-request login."""
    http = FakeHttpClient(token_posts=[_token_ok()])
    auth = make_auth(http, refresh_token="old-refresh")

    await auth.async_ensure_token()
    assert http.request_methods() == ["POST token"]
    grant = http.requests[0][2]
    assert grant["grant_type"] == "refresh_token"
    assert grant["refresh_token"] == "old-refresh"
    assert auth._access_token == "new-access-token"


async def test_rejected_refresh_token_falls_back_to_full_login():
    """A revoked refresh token must not surface as an auth failure — the full
    login can still succeed."""
    http = FakeHttpClient(
        authorize=_authorize_with_form(),
        form_post=_successful_form_post(),
        token_posts=[FakeResponse(status_code=400, json_data={}), _token_ok()],
    )
    auth = make_auth(http, refresh_token="revoked")

    await auth.async_ensure_token()
    assert http.request_methods() == [
        "POST token",  # refresh attempt, rejected
        "GET other",   # authorize
        "POST other",  # credentials
        "POST token",  # code exchange
    ]


async def test_live_sso_session_is_not_an_auth_failure():
    """Regression test.

    When a Keycloak SSO session is still alive, the authorize request is
    redirected straight to the redirect URI carrying ?code=... and no login
    form is rendered. Treating the missing form as a failure raised an auth
    error and prompted the user to re-enter credentials that were perfectly
    valid — precisely on the 401-retry path this code exists to serve."""
    http = FakeHttpClient(
        authorize=_authorize_with_sso_redirect("sso-123"), token_posts=[_token_ok()]
    )
    auth = make_auth(http)

    await auth.async_ensure_token()
    # No credential POST: the form was never needed.
    assert http.request_methods() == ["GET other", "POST token"]
    assert http.requests[-1][2]["code"] == "sso-123"


async def test_normal_login_posts_credentials_and_exchanges_the_code():
    http = FakeHttpClient(
        authorize=_authorize_with_form(),
        form_post=_successful_form_post("form-abc"),
        token_posts=[_token_ok()],
    )
    auth = make_auth(http)

    await auth.async_ensure_token()

    credentials = http.requests[1][2]
    assert credentials["username"] == "user@example.com"
    assert credentials["password"] == "s3cret"

    exchange = http.requests[2][2]
    assert exchange["grant_type"] == "authorization_code"
    assert exchange["code"] == "form-abc"
    # PKCE verifier must be sent, and within RFC 7636's length range.
    assert 43 <= len(exchange["code_verifier"]) <= 128


@pytest.mark.parametrize(
    "action",
    [
        # Another host entirely.
        "https://evil.example/iam/login-actions/authenticate?session_code=abc",
        # Brunata's host, but plaintext.
        "http://online.brunata.com/iam/login-actions/authenticate?session_code=abc",
        # Brunata's host over https, but a port nothing there listens on.
        "https://online.brunata.com:8443/iam/login-actions/authenticate?session_code=abc",
        # Relative. Reaches httpx as a URL with no host, where it raises
        # httpx.InvalidURL — which http.async_request() deliberately does
        # not translate, so it would arrive as a traceback instead of this.
        "/iam/login-actions/authenticate?session_code=abc",
    ],
)
async def test_a_login_form_pointing_elsewhere_never_sees_the_password(action):
    """The action attribute decides where the password goes.

    Keycloak writes it into the page, and nothing downstream reads it before it
    is used: the Location check runs on the *answer* to the credential POST, so
    by then the password has already left. An action that is not Brunata's has
    to be refused before the request, not after it.

    BrunataApiError, not BrunataAuthError, for the same reason as the missing
    form: the password was never wrong, so a reauth dialog would ask for one
    that already works.
    """
    html = (
        '<html><body><form id="kc-form-login" '
        f'action="{action}" method="post"></form></body></html>'
    )
    http = FakeHttpClient(authorize=FakeResponse(url=KC_AUTHORIZE_URL, text=html))
    auth = make_auth(http)

    with pytest.raises(BrunataApiError, match="login form posts to"):
        await auth.async_ensure_token()

    # The authorize GET and nothing else: no credential POST was made at all.
    assert http.request_methods() == ["GET other"]


async def test_force_clears_sso_cookies_and_skips_refresh():
    """force means the server rejected a token we believed was valid, so
    neither the SSO cookies nor the refresh token from that same session can be
    trusted to produce a working one."""
    http = FakeHttpClient(
        authorize=_authorize_with_form(),
        form_post=_successful_form_post(),
        token_posts=[_token_ok()],
    )
    auth = make_auth(
        http,
        access_token="rejected",
        refresh_token="same-session",
        expires_at=time.time() + 300,
    )

    await auth.async_ensure_token(force=True)
    assert http.cookies.cleared is True
    # No refresh attempt: the first request is the authorize GET.
    assert http.request_methods()[0] == "GET other"


async def test_wrong_credentials_raise_auth_error():
    """Keycloak re-renders the form (HTTP 200) instead of redirecting."""
    http = FakeHttpClient(
        authorize=_authorize_with_form(),
        form_post=FakeResponse(status_code=200, text=LOGIN_FORM_HTML),
    )
    auth = make_auth(http)

    # The message matters: a later guard would also reject this, but with an
    # error about the redirect target rather than the credentials.
    with pytest.raises(BrunataAuthError, match="check the email and password"):
        await auth.async_ensure_token()


async def test_missing_login_form_is_not_a_credentials_error():
    """No form and no code means the flow itself changed shape.

    It must not be a BrunataAuthError: that becomes ConfigEntryAuthFailed and
    opens a reauth dialog, so the user is asked to re-enter a password that was
    never wrong — and the next attempt fails in exactly the same place. A
    BrunataApiError becomes UpdateFailed, which retries and says what broke.
    """
    http = FakeHttpClient(
        authorize=FakeResponse(url=KC_AUTHORIZE_URL, text="<html></html>")
    )
    auth = make_auth(http)

    with pytest.raises(BrunataApiError):
        await auth.async_ensure_token()


async def test_unexpected_redirect_target_is_not_a_credentials_error():
    """Keycloak issued a redirect, so the credentials were accepted. Where it
    points is a question about the flow, not about the password."""
    http = FakeHttpClient(
        authorize=_authorize_with_form(),
        form_post=FakeResponse(
            status_code=302, headers={"Location": "https://elsewhere.example/?code=x"}
        ),
    )
    auth = make_auth(http)

    with pytest.raises(BrunataApiError):
        await auth.async_ensure_token()


async def test_redirect_without_code_is_not_a_credentials_error():
    http = FakeHttpClient(
        authorize=_authorize_with_form(),
        form_post=FakeResponse(
            status_code=302,
            headers={"Location": f"{KC_REDIRECT_URI}?error=access_denied"},
        ),
    )
    auth = make_auth(http)

    with pytest.raises(BrunataApiError):
        await auth.async_ensure_token()


async def test_token_endpoint_without_access_token_clears_state():
    http = FakeHttpClient(
        authorize=_authorize_with_form(),
        form_post=_successful_form_post(),
        token_posts=[_token_ok({"token_type": "Bearer"})],
    )
    # No refresh token, so the browser login runs and its token exchange is
    # the response under test.
    auth = make_auth(http, access_token="stale")

    with pytest.raises(BrunataAuthError):
        await auth.async_ensure_token()
    assert auth._access_token is None
    assert auth._refresh_token is None


@pytest.mark.parametrize("payload", [[], "an error page", 42, None])
async def test_a_token_response_that_is_not_an_object_is_refused(payload):
    """_payload() lets a body through that is not a JSON object.

    It reads the status and looks for an error body, but a list, a string or a
    bare null passes it untouched — and .get() on one of those raises
    AttributeError, which is not a type either caller of _store_tokens()
    catches. It escaped async_ensure_token(), escaped async_get_meters() and
    reached the coordinator as an unexpected exception with a traceback.

    Not far-fetched: a gateway, a captive portal or a bot-protection page can
    answer 200 with perfectly valid JSON that is not the token object.
    _async_ensure_lookup_tables() already makes this check on the locale
    resource; the two tests below are what the type of error buys.
    """
    http = FakeHttpClient()
    auth = make_auth(http)

    with pytest.raises(BrunataApiError, match="not an object"):
        auth._store_tokens(payload)


async def test_a_refresh_answered_with_the_wrong_shape_falls_back_to_a_full_login():
    """The half that mattered.

    The AttributeError escaped from inside _async_try_refresh() *before* the
    return that gives up on the refresh token, so the browser login below never
    ran. The same rejected token was replayed every hour, and only reloading
    the entry broke out of it. BrunataApiError is caught there and means
    "fall back", which is what the four requests below are.
    """
    http = FakeHttpClient(
        authorize=_authorize_with_form(),
        form_post=_successful_form_post(),
        token_posts=[FakeResponse(json_data=[]), _token_ok()],
    )
    auth = make_auth(http, refresh_token="old-refresh")

    await auth.async_ensure_token()

    assert http.request_methods() == [
        "POST token",  # refresh attempt, answered with a list
        "GET other",   # authorize
        "POST other",  # credentials
        "POST token",  # code exchange
    ]
    assert auth._access_token == "new-access-token"


async def test_a_token_exchange_answered_with_the_wrong_shape_clears_state():
    """The other caller. There is nothing left to fall back to here, so the
    same shape has to end as an auth error with no half-stored token behind
    it — exactly what a response carrying no access token already does."""
    http = FakeHttpClient(
        authorize=_authorize_with_form(),
        form_post=_successful_form_post(),
        token_posts=[FakeResponse(json_data=["not", "an", "object"])],
    )
    auth = make_auth(http, access_token="stale")

    with pytest.raises(BrunataAuthError):
        await auth.async_ensure_token()
    assert auth._access_token is None
    assert auth._refresh_token is None


async def test_expiry_is_derived_from_expires_in_only():
    """expires_at is ours to compute. A response carrying no expires_in must
    leave the token immediately unusable rather than inheriting the previous
    one's lifetime — otherwise a dead token is treated as good and the login it
    needed is skipped."""
    http = FakeHttpClient()
    auth = make_auth(http)

    auth._store_tokens({"access_token": "A", "expires_in": TOKEN_LIFETIME_SECONDS})
    assert auth._token_is_usable is True

    auth._store_tokens({"access_token": "B"})
    assert auth._token_is_usable is False


@pytest.mark.parametrize("expires_in", ["one hour", "", [], {}, True, False, float("inf")])
async def test_an_unreadable_expires_in_does_not_escape_the_login(expires_in):
    """_store_tokens() used to call float() on this directly.

    A string that is not a number raised ValueError and a list raised
    TypeError. Both callers catch only BrunataApiError around that call, so
    either one went straight out of async_ensure_token(), out of
    async_get_meters() and into the coordinator as an unexpected exception
    rather than as a clean retry or reauth.

    The right answer is the one an absent expires_in already gets: no usable
    expiry, so the token is not reused and the next request logs in again.
    That costs one login. The other way costs the update.

    True and False are in the list because bool subclasses int, so `true`
    would otherwise be read as a one-second lifetime.
    """
    http = FakeHttpClient()
    auth = make_auth(http)

    auth._store_tokens({"access_token": "A", "expires_in": expires_in})

    assert auth._access_token == "A"
    assert auth._token_is_usable is False


async def test_expiry_has_a_safety_margin():
    """A token expiring in a second must not be sent on a request that takes
    longer than that to complete."""
    http = FakeHttpClient()
    auth = make_auth(http)

    auth._store_tokens({"access_token": "A", "expires_in": 1})
    assert auth._token_is_usable is False

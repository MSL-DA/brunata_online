"""Tests for the Keycloak login that replaces brunata_api's own.

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
    BrunataApiClient,
    BrunataApiError,
    BrunataAuthError,
)

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


def make_client(http, *, access_token=None, refresh_token=None, expires_at=0.0):
    """Build a BrunataApiClient wired to a fake HTTP client."""
    client = BrunataApiClient("user@example.com", "s3cret", http)
    client._access_token = access_token
    client._token_type = "Bearer"
    client._refresh_token = refresh_token
    client._expires_at = expires_at
    return client


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
    """_async_login is called before every request; without the reuse check
    each one would trigger a full browser login."""
    http = FakeHttpClient()
    client = make_client(http, access_token="still-good", expires_at=time.time() + 300)

    await client._async_login()
    assert http.requests == []


async def test_expired_token_is_not_reused():
    http = FakeHttpClient(token_posts=[_token_ok()])
    client = make_client(
        http, access_token="expired", refresh_token="r", expires_at=time.time() - 1
    )

    await client._async_login()
    assert http.request_methods() == ["POST token"]


async def test_refresh_token_avoids_the_browser_flow():
    """An expired access token with a live refresh token costs one POST, not a
    three-request login."""
    http = FakeHttpClient(token_posts=[_token_ok()])
    client = make_client(http, refresh_token="old-refresh")

    await client._async_login()
    assert http.request_methods() == ["POST token"]
    grant = http.requests[0][2]
    assert grant["grant_type"] == "refresh_token"
    assert grant["refresh_token"] == "old-refresh"
    assert client._access_token == "new-access-token"


async def test_rejected_refresh_token_falls_back_to_full_login():
    """A revoked refresh token must not surface as an auth failure — the full
    login can still succeed."""
    http = FakeHttpClient(
        authorize=_authorize_with_form(),
        form_post=_successful_form_post(),
        token_posts=[FakeResponse(status_code=400, json_data={}), _token_ok()],
    )
    client = make_client(http, refresh_token="revoked")

    await client._async_login()
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
    client = make_client(http)

    await client._async_login()
    # No credential POST: the form was never needed.
    assert http.request_methods() == ["GET other", "POST token"]
    assert http.requests[-1][2]["code"] == "sso-123"


async def test_normal_login_posts_credentials_and_exchanges_the_code():
    http = FakeHttpClient(
        authorize=_authorize_with_form(),
        form_post=_successful_form_post("form-abc"),
        token_posts=[_token_ok()],
    )
    client = make_client(http)

    await client._async_login()

    credentials = http.requests[1][2]
    assert credentials["username"] == "user@example.com"
    assert credentials["password"] == "s3cret"

    exchange = http.requests[2][2]
    assert exchange["grant_type"] == "authorization_code"
    assert exchange["code"] == "form-abc"
    # PKCE verifier must be sent, and within RFC 7636's length range.
    assert 43 <= len(exchange["code_verifier"]) <= 128


async def test_force_clears_sso_cookies_and_skips_refresh():
    """force means the server rejected a token we believed was valid, so
    neither the SSO cookies nor the refresh token from that same session can be
    trusted to produce a working one."""
    http = FakeHttpClient(
        authorize=_authorize_with_form(),
        form_post=_successful_form_post(),
        token_posts=[_token_ok()],
    )
    client = make_client(
        http,
        access_token="rejected",
        refresh_token="same-session",
        expires_at=time.time() + 300,
    )

    await client._async_login(force=True)
    assert http.cookies.cleared is True
    # No refresh attempt: the first request is the authorize GET.
    assert http.request_methods()[0] == "GET other"


async def test_wrong_credentials_raise_auth_error():
    """Keycloak re-renders the form (HTTP 200) instead of redirecting."""
    http = FakeHttpClient(
        authorize=_authorize_with_form(),
        form_post=FakeResponse(status_code=200, text=LOGIN_FORM_HTML),
    )
    client = make_client(http)

    # The message matters: a later guard would also reject this, but with an
    # error about the redirect target rather than the credentials.
    with pytest.raises(BrunataAuthError, match="check the email and password"):
        await client._async_login()


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
    client = make_client(http)

    with pytest.raises(BrunataApiError):
        await client._async_login()


async def test_unexpected_redirect_target_is_not_a_credentials_error():
    """Keycloak issued a redirect, so the credentials were accepted. Where it
    points is a question about the flow, not about the password."""
    http = FakeHttpClient(
        authorize=_authorize_with_form(),
        form_post=FakeResponse(
            status_code=302, headers={"Location": "https://elsewhere.example/?code=x"}
        ),
    )
    client = make_client(http)

    with pytest.raises(BrunataApiError):
        await client._async_login()


async def test_redirect_without_code_is_not_a_credentials_error():
    http = FakeHttpClient(
        authorize=_authorize_with_form(),
        form_post=FakeResponse(
            status_code=302,
            headers={"Location": f"{KC_REDIRECT_URI}?error=access_denied"},
        ),
    )
    client = make_client(http)

    with pytest.raises(BrunataApiError):
        await client._async_login()


async def test_token_endpoint_without_access_token_clears_state():
    http = FakeHttpClient(
        authorize=_authorize_with_form(),
        form_post=_successful_form_post(),
        token_posts=[_token_ok({"token_type": "Bearer"})],
    )
    # No refresh token, so the browser login runs and its token exchange is
    # the response under test.
    client = make_client(http, access_token="stale")

    with pytest.raises(BrunataAuthError):
        await client._async_login()
    assert client._access_token is None
    assert client._refresh_token is None


async def test_expiry_is_derived_from_expires_in_only():
    """expires_at is ours to compute. A response carrying no expires_in must
    leave the token immediately unusable rather than inheriting the previous
    one's lifetime — otherwise a dead token is treated as good and the login it
    needed is skipped."""
    http = FakeHttpClient()
    client = make_client(http)

    client._store_tokens({"access_token": "A", "expires_in": TOKEN_LIFETIME_SECONDS})
    assert client._token_is_usable is True

    client._store_tokens({"access_token": "B"})
    assert client._token_is_usable is False


async def test_expiry_has_a_safety_margin():
    """A token expiring in a second must not be sent on a request that takes
    longer than that to complete."""
    http = FakeHttpClient()
    client = make_client(http)

    client._store_tokens({"access_token": "A", "expires_in": 1})
    assert client._token_is_usable is False

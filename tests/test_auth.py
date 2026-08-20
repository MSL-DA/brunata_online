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

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed

from custom_components.brunata import (
    KC_AUTHORIZE_URL,
    KC_CLIENT_ID,
    KC_REDIRECT_URI,
    KC_TOKEN_URL,
    _keycloak_get_tokens,
    _store_tokens,
)

LOGIN_FORM_HTML = (
    '<html><body><form id="kc-form-login" onsubmit="return true;" '
    'action="https://online.brunata.com/iam/login-actions/authenticate?'
    'session_code=abc&amp;execution=def" method="post">'
    '<input name="username"><input name="password"></form></body></html>'
)

TOKEN_RESPONSE = {
    "access_token": "new-access-token",
    "refresh_token": "new-refresh-token",
    "token_type": "Bearer",
    "expires_in": 300,
    "refresh_expires_in": 1800,
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

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"unexpected HTTP {self.status_code} in test")


class FakeCookies:
    def __init__(self):
        self.cleared = False

    def clear(self):
        self.cleared = True


class FakeSession:
    """Records requests and replays scripted responses."""

    def __init__(self, *, authorize=None, form_post=None, token_posts=None):
        self.headers = {}
        self.cookies = FakeCookies()
        self.requests = []
        self._authorize = authorize
        self._form_post = form_post
        self._token_posts = list(token_posts or [])

    async def get(self, url, **kwargs):
        self.requests.append(("GET", url))
        assert self._authorize is not None, "unexpected authorize request"
        return self._authorize

    async def post(self, url, **kwargs):
        self.requests.append(("POST", url, kwargs.get("data")))
        if url == KC_TOKEN_URL:
            assert self._token_posts, "unexpected token request"
            return self._token_posts.pop(0)
        assert self._form_post is not None, "unexpected credential POST"
        return self._form_post

    def request_methods(self):
        return [r[0] + " " + ("token" if r[1] == KC_TOKEN_URL else "other") for r in self.requests]


class FakeClient:
    """Stand-in for brunata_api.Client, exposing only what the code uses."""

    def __init__(self, session, *, tokens=None, token_valid=False):
        self._session = session
        self._username = "user@example.com"
        self._password = "s3cret"
        self._tokens = dict(tokens or {})
        self._token_valid = token_valid

    def _is_token_valid(self, _kind):
        return self._token_valid


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


async def test_valid_cached_token_skips_the_network_entirely():
    """The library calls _get_tokens() several times per update cycle; without
    the reuse check each call would perform a full browser login."""
    session = FakeSession()
    client = FakeClient(session, token_valid=True)
    client._session.headers["Authorization"] = "Bearer still-good"

    assert await _keycloak_get_tokens(client) is True
    assert session.requests == []


async def test_refresh_token_avoids_the_browser_flow():
    """An expired access token with a live refresh token costs one POST, not a
    three-request login."""
    session = FakeSession(token_posts=[_token_ok()])
    client = FakeClient(session, tokens={"refresh_token": "old-refresh"})

    assert await _keycloak_get_tokens(client) is True
    assert session.request_methods() == ["POST token"]
    grant = session.requests[0][2]
    assert grant["grant_type"] == "refresh_token"
    assert grant["refresh_token"] == "old-refresh"
    assert client._session.headers["Authorization"] == "Bearer new-access-token"


async def test_rejected_refresh_token_falls_back_to_full_login():
    """A revoked refresh token must not surface as an auth failure — the full
    login can still succeed."""
    session = FakeSession(
        authorize=_authorize_with_form(),
        form_post=_successful_form_post(),
        token_posts=[FakeResponse(status_code=400, json_data={}), _token_ok()],
    )
    client = FakeClient(session, tokens={"refresh_token": "revoked"})

    assert await _keycloak_get_tokens(client) is True
    assert session.request_methods() == [
        "POST token",  # refresh attempt, rejected
        "GET other",  # authorize
        "POST other",  # credentials
        "POST token",  # code exchange
    ]


async def test_live_sso_session_is_not_an_auth_failure():
    """Regression test.

    When a Keycloak SSO session is still alive, the authorize request is
    redirected straight to the redirect URI carrying ?code=... and no login
    form is rendered. Treating the missing form as a failure raised
    ConfigEntryAuthFailed and prompted the user to re-enter credentials that
    were perfectly valid — precisely on the 401-retry path this code exists to
    serve."""
    session = FakeSession(
        authorize=_authorize_with_sso_redirect("sso-123"), token_posts=[_token_ok()]
    )
    client = FakeClient(session)

    assert await _keycloak_get_tokens(client) is True
    # No credential POST: the form was never needed.
    assert session.request_methods() == ["GET other", "POST token"]
    assert session.requests[-1][2]["code"] == "sso-123"


async def test_normal_login_posts_credentials_and_exchanges_the_code():
    session = FakeSession(
        authorize=_authorize_with_form(),
        form_post=_successful_form_post("form-abc"),
        token_posts=[_token_ok()],
    )
    client = FakeClient(session)

    assert await _keycloak_get_tokens(client) is True

    credentials = session.requests[1][2]
    assert credentials["username"] == "user@example.com"
    assert credentials["password"] == "s3cret"

    exchange = session.requests[2][2]
    assert exchange["grant_type"] == "authorization_code"
    assert exchange["code"] == "form-abc"
    # PKCE verifier must be sent, and must be within RFC 7636's length range.
    assert 43 <= len(exchange["code_verifier"]) <= 128


async def test_force_clears_sso_cookies_and_skips_refresh():
    """force=True means the server rejected a token we believed was valid, so
    neither the SSO cookies nor the refresh token from that same session can be
    trusted to produce a working one."""
    session = FakeSession(
        authorize=_authorize_with_form(),
        form_post=_successful_form_post(),
        token_posts=[_token_ok()],
    )
    client = FakeClient(session, tokens={"refresh_token": "same-session"}, token_valid=True)
    client._session.headers["Authorization"] = "Bearer rejected"

    assert await _keycloak_get_tokens(client, force=True) is True
    assert session.cookies.cleared is True
    # No refresh attempt: the first request is the authorize GET.
    assert session.request_methods()[0] == "GET other"


async def test_wrong_credentials_raise_auth_failed():
    """Keycloak re-renders the form (HTTP 200) instead of redirecting."""
    session = FakeSession(
        authorize=_authorize_with_form(),
        form_post=FakeResponse(status_code=200, text=LOGIN_FORM_HTML),
    )
    client = FakeClient(session)

    with pytest.raises(ConfigEntryAuthFailed):
        await _keycloak_get_tokens(client)


async def test_missing_login_form_raises_auth_failed():
    """No form and no code means the flow itself changed shape."""
    session = FakeSession(authorize=FakeResponse(url=KC_AUTHORIZE_URL, text="<html></html>"))
    client = FakeClient(session)

    with pytest.raises(ConfigEntryAuthFailed):
        await _keycloak_get_tokens(client)


async def test_unexpected_redirect_target_raises_auth_failed():
    session = FakeSession(
        authorize=_authorize_with_form(),
        form_post=FakeResponse(
            status_code=302, headers={"Location": "https://elsewhere.example/?code=x"}
        ),
    )
    client = FakeClient(session)

    with pytest.raises(ConfigEntryAuthFailed):
        await _keycloak_get_tokens(client)


async def test_redirect_without_code_raises_auth_failed():
    session = FakeSession(
        authorize=_authorize_with_form(),
        form_post=FakeResponse(
            status_code=302, headers={"Location": f"{KC_REDIRECT_URI}?error=access_denied"}
        ),
    )
    client = FakeClient(session)

    with pytest.raises(ConfigEntryAuthFailed):
        await _keycloak_get_tokens(client)


async def test_token_endpoint_without_access_token_clears_state():
    session = FakeSession(
        authorize=_authorize_with_form(),
        form_post=_successful_form_post(),
        token_posts=[_token_ok({"token_type": "Bearer"})],
    )
    client = FakeClient(session, tokens={"access_token": "stale"})

    with pytest.raises(ConfigEntryAuthFailed):
        await _keycloak_get_tokens(client)
    assert client._tokens == {}


async def test_store_tokens_does_not_let_stale_expiry_survive():
    """Regression test.

    Merging into the previous token dict let an old expires_on survive a
    response that carried no expires_in, so _is_token_valid() could report a
    dead token as usable and the caller would skip the login it needed."""
    session = FakeSession()
    client = FakeClient(session)

    _store_tokens(client, {"access_token": "A", "expires_in": 300, "refresh_expires_in": 1800})
    assert "expires_on" in client._tokens
    assert "refresh_token_expires_on" in client._tokens
    first = client._tokens["expires_on"]

    _store_tokens(client, {"access_token": "B"})
    assert "expires_on" not in client._tokens
    assert "refresh_token_expires_on" not in client._tokens
    assert client._tokens["access_token"] == "B"
    assert first is not None


async def test_store_tokens_discards_an_expiry_it_did_not_compute():
    """expires_on is our own derived field, not part of the OAuth response.

    A payload carrying one but no expires_in must not be able to dictate the
    token's lifetime — a far-future value would make _is_token_valid() report
    a dead token as usable indefinitely."""
    session = FakeSession()
    client = FakeClient(session)

    _store_tokens(client, {"access_token": "A", "expires_on": 99_999_999_999})
    assert "expires_on" not in client._tokens

    # With a real expires_in, the value is recomputed rather than trusted.
    _store_tokens(client, {"access_token": "B", "expires_in": 300, "expires_on": 1})
    assert client._tokens["expires_on"] > 1


async def test_store_tokens_keeps_the_dict_identity():
    """The library may hold a reference to _tokens, so it must be mutated in
    place rather than rebound."""
    session = FakeSession()
    client = FakeClient(session)
    original = client._tokens

    _store_tokens(client, {"access_token": "A", "expires_in": 60})
    assert client._tokens is original

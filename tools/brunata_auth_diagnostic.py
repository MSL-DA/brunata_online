#!/usr/bin/env python3
"""One-shot Brunata Keycloak auth diagnostic.

Logs in with real credentials and compares two ways of exchanging the
authorization code for tokens:

  A) directly at Keycloak's /token endpoint (what the integration does now)
  B) at Brunata's /online-auth-webservice proxy /oauth/token (what the web app does)

For each, it decodes the access-token `aud` claim and calls /consumer/meters
on both v1 and v2 of the webservice, printing the HTTP status. Whichever yields
200 on /consumer/meters is the correct flow.

Run on a machine that has httpx (e.g. the HA container)::

    BRUNATA_EMAIL='you@example.com' BRUNATA_PASSWORD='secret' python brunata_auth_diagnostic.py

Nothing is written anywhere; credentials are only used in-memory.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import json
import os
import re
import sys
from urllib.parse import parse_qs, urlparse

import httpx

BASE = "https://online.brunata.com"
KC = f"{BASE}/iam/realms/online-prod/protocol/openid-connect"
KC_AUTH = f"{KC}/auth"
KC_TOKEN = f"{KC}/token"
PROXY_TOKEN = f"{BASE}/online-auth-webservice/v1/rest/oauth/token"
CLIENT_ID = "82770188-c92e-4d16-927d-a15c472eda55"
REDIRECT = f"{BASE}/auth-redirect"
FORM_RE = re.compile(r'id="kc-form-login"[^>]*action="([^"]+)"', re.IGNORECASE)
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept-Language": "en"}


def _pkce() -> tuple[str, str]:
    verifier = re.sub("[^a-zA-Z0-9]+", "", base64.urlsafe_b64encode(os.urandom(40)).decode())
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    return verifier, challenge


def _decode_aud(access_token: str) -> object:
    try:
        payload = access_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return {"aud": claims.get("aud"), "azp": claims.get("azp"), "scope": claims.get("scope")}
    except Exception as err:  # noqa: BLE001
        return f"<could not decode: {err}>"


async def _login_get_code(session: httpx.AsyncClient, email: str, password: str) -> tuple[str, str]:
    """Run authorize + credential POST, return (auth_code, code_verifier)."""
    verifier, challenge = _pkce()
    page = await session.get(
        KC_AUTH,
        params={
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT,
            "scope": "openid offline_access",
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
        follow_redirects=True,
    )
    page.raise_for_status()
    m = FORM_RE.search(page.text)
    if not m:
        raise RuntimeError("login form not found (SSO redirect or flow changed)")
    action = html.unescape(m.group(1))
    auth = await session.post(
        action,
        data={"username": email, "password": password, "credentialId": ""},
        follow_redirects=False,
    )
    if auth.status_code not in (301, 302, 303, 307, 308):
        raise RuntimeError(f"credential POST not redirected (status {auth.status_code}) — bad credentials?")
    loc = auth.headers.get("Location", "")
    code = parse_qs(urlparse(loc).query).get("code", [None])[0]
    if not code:
        raise RuntimeError(f"no code in redirect: {loc[:120]}")
    return code, verifier


async def _test_meters(token_type: str, access_token: str) -> None:
    auth_header = f"{token_type or 'Bearer'} {access_token}"
    print("    aud/scope:", _decode_aud(access_token))
    async with httpx.AsyncClient(timeout=15, headers={**HEADERS, "Authorization": auth_header}) as s:
        for ver in ("v1", "v2"):
            url = f"{BASE}/online-webservice/{ver}/rest/consumer/meters"
            r = await s.get(url, headers={"Referer": f"{BASE}/react-online/meters-values"})
            body = r.text[:120].replace("\n", " ")
            print(f"    {ver} /consumer/meters -> {r.status_code}   {body}")


def _load_dotenv() -> None:
    """Load KEY=VALUE pairs from a .env file (repo root or cwd) into os.environ.

    Existing environment variables take precedence; quotes are stripped.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(os.getcwd(), ".env"),
        os.path.join(here, ".env"),
        os.path.join(os.path.dirname(here), ".env"),  # repo root (tools/..)
    ]
    for path in candidates:
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
        print(f"(loaded credentials from {path})")
        return


async def main() -> int:
    _load_dotenv()
    email = os.environ.get("BRUNATA_EMAIL")
    password = os.environ.get("BRUNATA_PASSWORD")
    if not email or not password:
        print("Set BRUNATA_EMAIL and BRUNATA_PASSWORD (env vars or a .env file).")
        return 2

    print("=== Approach A: exchange code at KEYCLOAK /token (current integration behaviour) ===")
    try:
        async with httpx.AsyncClient(timeout=15, headers=HEADERS) as session:
            code, verifier = await _login_get_code(session, email, password)
            tok = await session.post(
                KC_TOKEN,
                data={
                    "grant_type": "authorization_code",
                    "client_id": CLIENT_ID,
                    "redirect_uri": REDIRECT,
                    "code": code,
                    "code_verifier": verifier,
                },
                follow_redirects=False,
            )
            print("    token endpoint status:", tok.status_code)
            tok.raise_for_status()
            data = tok.json()
            await _test_meters(data.get("token_type", "Bearer"), data["access_token"])
    except Exception as err:  # noqa: BLE001
        print("    Approach A failed:", err)

    print()
    print("=== Approach B: exchange code at PROXY /oauth/token (web app behaviour) ===")
    try:
        async with httpx.AsyncClient(timeout=15, headers=HEADERS) as session:
            code, verifier = await _login_get_code(session, email, password)
            tok = await session.post(
                PROXY_TOKEN,
                data={
                    "grant_type": "authorization_code",
                    "client_id": CLIENT_ID,
                    "redirect_uri": REDIRECT,
                    "scope": f"{CLIENT_ID} offline_access",
                    "code": code,
                    "code_verifier": verifier,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                follow_redirects=False,
            )
            print("    token endpoint status:", tok.status_code)
            tok.raise_for_status()
            data = tok.json()
            await _test_meters(data.get("token_type", "Bearer"), data["access_token"])
    except Exception as err:  # noqa: BLE001
        print("    Approach B failed:", err)

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

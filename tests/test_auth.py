"""Tests for the Auth0 login and token handling."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
from datetime import UTC, datetime, timedelta

import aiohttp
import pytest

from custom_components.dots_energy.auth import (
    AuthError,
    CannotConnect,
    CaptchaRequired,
    DotsAuth,
    InvalidCredentials,
    TokenSet,
)

from .auth_mocks import (
    AUTHORIZE_RE,
    CAPTCHA_HTML,
    LOGIN,
    LOGIN_HTML,
    TOKEN,
    mock_login,
    sequence,
    token_payload,
)

EMAIL = "someone@example.invalid"
PASSWORD = "s3cret-Passw0rd!"


class Recorder:
    """Collect tokens passed to on_tokens."""

    def __init__(self) -> None:
        self.tokens: list[TokenSet] = []

    def __call__(self, tokens: TokenSet) -> None:
        self.tokens.append(tokens)


@pytest.fixture
async def make_auth(aioclient_mock):
    loop = asyncio.get_running_loop
    sessions: list[aiohttp.ClientSession] = []

    def _session() -> aiohttp.ClientSession:
        session = aioclient_mock.create_session(loop())
        sessions.append(session)
        return session

    def _make(**kwargs) -> DotsAuth:
        kwargs.setdefault("email", EMAIL)
        kwargs.setdefault("password", PASSWORD)
        kwargs.setdefault("refresh_token", None)
        kwargs.setdefault("on_tokens", Recorder())
        return DotsAuth(_session(), login_session_factory=_session, **kwargs)

    yield _make
    for session in sessions:
        await session.close()


def _posts(aioclient_mock, url: str) -> int:
    return sum(1 for m, u, _, _ in aioclient_mock.mock_calls if m.lower() == "post" and str(u) == url)


async def test_login_happy_path(aioclient_mock, make_auth) -> None:
    rec = Recorder()
    auth = make_auth(on_tokens=rec)
    captured = mock_login(aioclient_mock)

    tokens = await auth.async_login()

    assert tokens.access_token == "AT-login"
    assert tokens.refresh_token == "RT-login"
    assert tokens.expires_at > datetime.now(UTC) + timedelta(hours=23)
    assert rec.tokens == [tokens]
    query = captured["authorize"]
    assert query["client_id"] == ["VFklW8ALID2BlM7pMp2MCELFI635nhG2"]
    assert query["redirect_uri"] == ["https://my.dotsenergy.be"]
    assert query["audience"] == ["https://my.api.dotsenergy.be"]
    assert query["code_challenge_method"] == ["S256"]
    assert captured["form"] == {
        "state": "ST1",
        "username": EMAIL,
        "password": PASSWORD,
        "action": "default",
    }
    body = captured["token"]
    assert body["grant_type"] == "authorization_code"
    assert body["code"] == "CODE1"
    assert body["redirect_uri"] == "https://my.dotsenergy.be"
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(body["code_verifier"].encode()).digest())
        .decode()
        .rstrip("=")
    )
    assert query["code_challenge"] == [expected]


def _login_page(aioclient_mock, html: str = LOGIN_HTML) -> None:
    aioclient_mock.get(AUTHORIZE_RE, status=302, headers={"Location": "/u/login?state=ST1"})
    aioclient_mock.get(LOGIN, text=html, headers={"Content-Type": "text/html"})


async def test_login_wrong_password_raises_invalid(aioclient_mock, make_auth) -> None:
    _login_page(aioclient_mock)
    aioclient_mock.post(LOGIN, status=400, text=LOGIN_HTML)
    with pytest.raises(InvalidCredentials):
        await make_auth(password="wrong").async_login()


async def test_login_captcha_raises_captcha_required(aioclient_mock, make_auth) -> None:
    _login_page(aioclient_mock, CAPTCHA_HTML)
    with pytest.raises(CaptchaRequired):
        await make_auth().async_login()
    assert _posts(aioclient_mock, LOGIN) == 0


async def test_login_captcha_after_post_raises_captcha_required(aioclient_mock, make_auth) -> None:
    _login_page(aioclient_mock)
    aioclient_mock.post(LOGIN, status=400, text=CAPTCHA_HTML)
    with pytest.raises(CaptchaRequired):
        await make_auth().async_login()


async def test_login_state_mismatch_raises_autherror(aioclient_mock, make_auth) -> None:
    mock_login(aioclient_mock, state_override="forged-state")
    with pytest.raises(AuthError):
        await make_auth().async_login()


async def test_login_connection_error_raises_cannot_connect(aioclient_mock, make_auth) -> None:
    aioclient_mock.get(AUTHORIZE_RE, exc=aiohttp.ClientConnectionError("boom"))
    with pytest.raises(CannotConnect):
        await make_auth().async_login()


async def test_refresh_rotates_token(aioclient_mock, make_auth) -> None:
    rec = Recorder()
    auth = make_auth(email=None, password=None, refresh_token="RT-old", on_tokens=rec)
    aioclient_mock.post(TOKEN, json=token_payload("AT-new", "RT-new", 3600))

    tokens = await auth.async_refresh()

    sent = aioclient_mock.mock_calls[-1][2]
    assert sent["grant_type"] == "refresh_token"
    assert sent["refresh_token"] == "RT-old"
    assert sent["client_id"] == "VFklW8ALID2BlM7pMp2MCELFI635nhG2"
    assert tokens.refresh_token == "RT-new"
    assert rec.tokens[-1].refresh_token == "RT-new"


async def test_refresh_without_rotation_keeps_old_refresh_token(aioclient_mock, make_auth) -> None:
    auth = make_auth(email=None, password=None, refresh_token="RT-old")
    aioclient_mock.post(TOKEN, json=token_payload("AT-new", None, 3600))
    assert (await auth.async_refresh()).refresh_token == "RT-old"


async def test_get_access_token_uses_cached_token(aioclient_mock, make_auth) -> None:
    auth = make_auth(email=None, password=None, refresh_token="RT-old")
    aioclient_mock.post(TOKEN, json=token_payload("AT-1", "RT-1", 3600))
    assert await auth.async_get_access_token() == "AT-1"
    assert await auth.async_get_access_token() == "AT-1"
    assert _posts(aioclient_mock, TOKEN) == 1


async def test_force_refresh_refreshes_even_if_valid(aioclient_mock, make_auth) -> None:
    auth = make_auth(email=None, password=None, refresh_token="RT-old")
    aioclient_mock.post(
        TOKEN,
        side_effect=sequence(
            {"json": token_payload("AT-1", "RT-1", 3600)},
            {"json": token_payload("AT-2", "RT-2", 3600)},
        ),
    )
    assert await auth.async_get_access_token() == "AT-1"
    assert await auth.async_get_access_token(force_refresh=True) == "AT-2"


async def test_get_access_token_falls_back_to_login_when_refresh_invalid_grant(
    aioclient_mock, make_auth
) -> None:
    rec = Recorder()
    auth = make_auth(refresh_token="RT-dead", on_tokens=rec)
    captured = mock_login(
        aioclient_mock,
        refresh_grant_response={"status": 403, "json": {"error": "invalid_grant"}},
    )

    assert await auth.async_get_access_token() == "AT-login"
    assert rec.tokens[-1].refresh_token == "RT-login"
    assert captured["form"]["username"] == EMAIL


async def test_get_access_token_without_password_raises_on_invalid_grant(
    aioclient_mock, make_auth
) -> None:
    auth = make_auth(email=None, password=None, refresh_token="RT-dead")
    aioclient_mock.post(TOKEN, status=403, json={"error": "invalid_grant"})
    with pytest.raises(InvalidCredentials):
        await auth.async_get_access_token()


async def test_refresh_server_error_raises_cannot_connect(aioclient_mock, make_auth) -> None:
    auth = make_auth(refresh_token="RT-x")
    aioclient_mock.post(TOKEN, status=503, text="unavailable")
    with pytest.raises(CannotConnect):
        await auth.async_get_access_token()


async def test_no_secrets_in_logs(aioclient_mock, make_auth, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    mock_login(aioclient_mock)
    await make_auth().async_login()
    aioclient_mock.clear_requests()
    _login_page(aioclient_mock)
    aioclient_mock.post(LOGIN, status=400, text=LOGIN_HTML)
    with pytest.raises(InvalidCredentials):
        await make_auth().async_login()

    for secret in (PASSWORD, EMAIL, "AT-login", "RT-login", "CODE1"):
        assert secret not in caplog.text

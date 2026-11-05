"""Helpers that mock the Auth0 Universal Login round trip."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs

from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

AUTH = "https://auth.dotsenergy.be"
AUTHORIZE_RE = re.compile(r"^https://auth\.dotsenergy\.be/authorize\?")
LOGIN = f"{AUTH}/u/login?state=ST1"
RESUME = f"{AUTH}/authorize/resume?state=ST2"
TOKEN = f"{AUTH}/oauth/token"

LOGIN_HTML = (
    '<form method="POST"><input type="hidden" name="state" value="ST1">'
    '<input name="username"><input name="password" type="password">'
    '<button type="submit" name="action" value="default">Doorgaan</button></form>'
    '<script>var p="arkose"; q("div[data-captcha-sitekey]")</script>'
)
CAPTCHA_HTML = LOGIN_HTML.replace(
    "<button",
    '<div class="captcha" data-captcha-sitekey="site-key" '
    'data-captcha-provider="auth0_v2"></div><button',
)


def redirect(method: str, url: Any, location: str) -> AiohttpClientMockResponse:
    """Build a 302 response."""
    return AiohttpClientMockResponse(method, url, status=302, headers={"Location": location})


def token_payload(access: str, refresh: str | None, expires_in: int = 86400) -> dict:
    """Build an Auth0 token response body."""
    body: dict[str, Any] = {
        "access_token": access,
        "expires_in": expires_in,
        "token_type": "Bearer",
    }
    if refresh:
        body["refresh_token"] = refresh
    return body


def sequence(*responses: dict[str, Any]):
    """Side effect returning the given responses in order (last one repeats)."""
    remaining = list(responses)

    async def side_effect(method, url, data):
        spec = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        return AiohttpClientMockResponse(method, url, **spec)

    return side_effect


def mock_login(
    mocker: AiohttpClientMocker,
    *,
    state_override: str | None = None,
    access: str = "AT-login",
    refresh: str = "RT-login",
    refresh_grant_response: dict[str, Any] | None = None,
) -> dict:
    """Register a successful login round trip and return what was sent."""
    captured: dict[str, Any] = {}

    async def authorize(method, url, data):
        captured["authorize"] = parse_qs(url.query_string)
        return redirect(method, url, "/u/login?state=ST1")

    async def login_post(method, url, data):
        captured["form"] = dict(data)
        return redirect(method, url, "/authorize/resume?state=ST2")

    async def resume(method, url, data):
        state = state_override or captured["authorize"]["state"][0]
        return redirect(method, url, f"https://my.dotsenergy.be?code=CODE1&state={state}")

    async def token(method, url, data):
        if refresh_grant_response is not None and data.get("grant_type") == "refresh_token":
            return AiohttpClientMockResponse(method, url, **refresh_grant_response)
        captured["token"] = data
        return AiohttpClientMockResponse(method, url, json=token_payload(access, refresh))

    mocker.get(AUTHORIZE_RE, side_effect=authorize)
    mocker.get(LOGIN, text=LOGIN_HTML, headers={"Content-Type": "text/html"})
    mocker.post(LOGIN, side_effect=login_post)
    mocker.get(RESUME, side_effect=resume)
    mocker.post(TOKEN, side_effect=token)
    return captured

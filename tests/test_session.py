"""The per-login session must be released without closing HA's shared connector."""

from __future__ import annotations

import aiohttp

from custom_components.dots_energy.session import ha_login_session


async def test_login_session_releases_cleanly(hass, caplog) -> None:
    async with ha_login_session(hass) as session:
        assert isinstance(session, aiohttp.ClientSession)
        assert isinstance(session.cookie_jar, aiohttp.CookieJar)

    assert session.closed
    assert "closes the Home Assistant aiohttp session" not in caplog.text


async def test_two_logins_get_separate_cookie_jars(hass) -> None:
    async with ha_login_session(hass) as first, ha_login_session(hass) as second:
        assert first.cookie_jar is not second.cookie_jar

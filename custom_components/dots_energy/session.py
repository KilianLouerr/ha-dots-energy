"""Short-lived aiohttp session for one Universal Login round trip."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession


@asynccontextmanager
async def ha_login_session(hass: HomeAssistant) -> AsyncIterator[aiohttp.ClientSession]:
    """Session with its own cookie jar on HA's shared connector.

    HA forbids closing its sessions; a session created with auto_cleanup=False
    must be detached instead, which releases it without closing the connector.
    """
    session = async_create_clientsession(hass, auto_cleanup=False, cookie_jar=aiohttp.CookieJar())
    try:
        yield session
    finally:
        session.detach()

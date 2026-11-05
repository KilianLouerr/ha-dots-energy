"""The Dots Energy integration."""

from __future__ import annotations

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession, async_get_clientsession

from .api import DotsApiClient
from .auth import DotsAuth, TokenSet
from .const import CONF_LOCATION_ID, CONF_REFRESH_TOKEN
from .coordinator import DotsCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.SELECT, Platform.EVENT]

type DotsConfigEntry = ConfigEntry[DotsCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: DotsConfigEntry) -> bool:
    """Set up Dots Energy from a config entry."""
    session = async_get_clientsession(hass)

    def on_tokens(tokens: TokenSet) -> None:
        if tokens.refresh_token and tokens.refresh_token != entry.data.get(CONF_REFRESH_TOKEN):
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_REFRESH_TOKEN: tokens.refresh_token}
            )

    auth = DotsAuth(
        session,
        email=entry.data.get(CONF_EMAIL) or None,
        password=entry.data.get(CONF_PASSWORD) or None,
        refresh_token=entry.data.get(CONF_REFRESH_TOKEN) or None,
        on_tokens=on_tokens,
        login_session_factory=lambda: async_create_clientsession(
            hass, auto_cleanup=False, cookie_jar=aiohttp.CookieJar()
        ),
    )
    coordinator = DotsCoordinator(
        hass, entry, DotsApiClient(session, auth), int(entry.data[CONF_LOCATION_ID])
    )
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: DotsConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

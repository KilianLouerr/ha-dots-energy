"""Config flow for Dots Energy."""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import ApiError, DotsApiClient
from .auth import AuthError, CannotConnect, CaptchaRequired, DotsAuth, InvalidCredentials, TokenSet
from .const import CONF_LOCATION_ID, CONF_REFRESH_TOKEN, DOMAIN, REDIRECT_URI
from .session import ha_login_session

_LOGGER = logging.getLogger(__name__)

_PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


def _credentials_schema(email: str = "") -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_EMAIL, default=email): TextSelector(
                TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
            ),
            vol.Required(CONF_PASSWORD): _PASSWORD_SELECTOR,
        }
    )


def subject_from_token(access_token: str) -> str | None:
    """Read the Auth0 'sub' claim (no signature check; used only as an id)."""
    try:
        payload = access_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("sub")
    except IndexError, ValueError, AttributeError:
        return None


class DotsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up Dots Energy with e-mail and password, or a refresh token."""

    VERSION = 1

    def __init__(self) -> None:
        self._email = ""
        self._password = ""
        self._auth: DotsAuth | None = None
        self._tokens: TokenSet | None = None
        self._locations: list[dict[str, Any]] = []

    def _on_tokens(self, tokens: TokenSet) -> None:
        self._tokens = tokens

    def _make_auth(self, email: str, password: str, refresh_token: str | None) -> DotsAuth:
        hass = self.hass
        return DotsAuth(
            async_get_clientsession(hass),
            email=email or None,
            password=password or None,
            refresh_token=refresh_token,
            on_tokens=self._on_tokens,
            login_session_factory=lambda: ha_login_session(hass),
        )

    async def _async_try_password(
        self, user_input: dict[str, Any], step_id: str
    ) -> ConfigFlowResult:
        self._email = user_input[CONF_EMAIL].strip()
        self._password = user_input[CONF_PASSWORD]
        self._auth = self._make_auth(self._email, self._password, None)
        errors: dict[str, str] = {}
        try:
            await self._auth.async_login()
        except CaptchaRequired:
            return await self.async_step_captcha()
        except InvalidCredentials:
            errors["base"] = "invalid_auth"
        except CannotConnect:
            errors["base"] = "cannot_connect"
        except AuthError as err:
            _LOGGER.warning("Unexpected Dots login response: %s", err)
            return await self.async_step_login_unknown()
        else:
            return await self._async_finish()
        return self.async_show_form(
            step_id=step_id, data_schema=_credentials_schema(self._email), errors=errors
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the Dots portal e-mail and password."""
        if user_input is not None:
            return await self._async_try_password(user_input, "user")
        return self.async_show_form(step_id="user", data_schema=_credentials_schema())

    def _fallback_menu(self, step_id: str) -> ConfigFlowResult:
        back = "reauth_confirm" if self.source == SOURCE_REAUTH else "user"
        return self.async_show_menu(step_id=step_id, menu_options=["refresh_token", back])

    async def async_step_captcha(self, user_input: None = None) -> ConfigFlowResult:
        """Dots asks for a captcha: offer the refresh-token route."""
        return self._fallback_menu("captcha")

    async def async_step_login_unknown(self, user_input: None = None) -> ConfigFlowResult:
        """Dots answered the login unexpectedly: offer the refresh-token route."""
        return self._fallback_menu("login_unknown")

    async def async_step_refresh_token(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Log in with a refresh token from a separate browser session."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._email = ""
            self._password = ""
            self._auth = self._make_auth("", "", user_input[CONF_REFRESH_TOKEN].strip())
            try:
                await self._auth.async_refresh()
            except InvalidCredentials:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except AuthError as err:
                _LOGGER.warning("Unexpected Dots token response: %s", err)
                errors["base"] = "unknown"
            else:
                return await self._async_finish()
        return self.async_show_form(
            step_id="refresh_token",
            data_schema=vol.Schema({vol.Required(CONF_REFRESH_TOKEN): _PASSWORD_SELECTOR}),
            errors=errors,
            description_placeholders={"portal_url": REDIRECT_URI},
        )

    async def _async_finish(self) -> ConfigFlowResult:
        assert self._auth is not None and self._tokens is not None
        unique_id = subject_from_token(self._tokens.access_token) or self._email.lower() or None
        credentials = {
            CONF_EMAIL: self._email,
            CONF_PASSWORD: self._password,
            CONF_REFRESH_TOKEN: self._tokens.refresh_token,
        }
        await self.async_set_unique_id(unique_id)
        if self.source == SOURCE_REAUTH:
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(), data_updates=credentials
            )
        self._abort_if_unique_id_configured()
        api = DotsApiClient(async_get_clientsession(self.hass), self._auth)
        try:
            self._locations = await api.async_get_locations()
        except ApiError, AuthError:
            return self.async_abort(reason="cannot_connect")
        if not self._locations:
            return self.async_abort(reason="no_locations")
        if len(self._locations) == 1:
            return self._create(self._locations[0]["id"])
        return await self.async_step_location()

    def _create(self, location_id: int) -> ConfigFlowResult:
        assert self._tokens is not None
        return self.async_create_entry(
            title="Dots Energy",
            data={
                CONF_EMAIL: self._email,
                CONF_PASSWORD: self._password,
                CONF_REFRESH_TOKEN: self._tokens.refresh_token,
                CONF_LOCATION_ID: location_id,
            },
        )

    async def async_step_location(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick a delivery location when the account has more than one."""
        if user_input is not None:
            return self._create(int(user_input[CONF_LOCATION_ID]))
        options = [{"value": str(loc["id"]), "label": loc["name"]} for loc in self._locations]
        return self.async_show_form(
            step_id="location",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_LOCATION_ID): SelectSelector(
                        SelectSelectorConfig(options=options)
                    )
                }
            ),
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Dots no longer accepts the stored login."""
        self._email = entry_data.get(CONF_EMAIL, "")
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the password again (or offer the refresh-token route)."""
        if user_input is not None:
            return await self._async_try_password(user_input, "reauth_confirm")
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=_credentials_schema(self._email)
        )

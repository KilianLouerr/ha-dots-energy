"""Tests for the config flow."""

from __future__ import annotations

import base64
import json
from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dots_energy.const import CONF_LOCATION_ID, CONF_REFRESH_TOKEN, DOMAIN

from .auth_mocks import (
    AUTHORIZE_RE,
    CAPTCHA_HTML,
    LOGIN,
    LOGIN_HTML,
    TOKEN,
    mock_login,
    token_payload,
)

API = "https://api.dotsenergy.be"
EMAIL = "someone@example.invalid"
PASSWORD = "s3cret-Passw0rd!"


def jwt(sub: str) -> str:
    def enc(obj) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{enc({'alg': 'RS256'})}.{enc({'sub': sub})}.signature"


@pytest.fixture(autouse=True)
def bypass_setup():
    with patch(
        "custom_components.dots_energy.async_setup_entry", return_value=True, create=True
    ) as mock:
        yield mock


def _locations(aioclient_mock, *locations) -> None:
    aioclient_mock.get(
        f"{API}/v1/my/locations", json=list(locations) or [{"id": 7, "name": "Thuis"}]
    )


def _login_page(aioclient_mock, html=LOGIN_HTML) -> None:
    aioclient_mock.get(AUTHORIZE_RE, status=302, headers={"Location": "/u/login?state=ST1"})
    aioclient_mock.get(LOGIN, text=html, headers={"Content-Type": "text/html"})


async def _start(hass):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def test_user_flow_creates_entry(hass, aioclient_mock) -> None:
    mock_login(aioclient_mock, access=jwt("auth0|user-1"), refresh="RT-1")
    _locations(aioclient_mock)

    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Dots Energy"
    assert result["data"] == {
        CONF_EMAIL: EMAIL,
        CONF_PASSWORD: PASSWORD,
        CONF_REFRESH_TOKEN: "RT-1",
        CONF_LOCATION_ID: 7,
    }
    assert result["result"].unique_id == "auth0|user-1"


async def test_invalid_auth_shows_error(hass, aioclient_mock) -> None:
    _login_page(aioclient_mock)
    aioclient_mock.post(LOGIN, status=400, text=LOGIN_HTML)

    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: EMAIL, CONF_PASSWORD: "wrong"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_cannot_connect_shows_error(hass, aioclient_mock) -> None:
    aioclient_mock.get(AUTHORIZE_RE, status=503)

    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}
    )

    assert result["errors"] == {"base": "cannot_connect"}


async def test_captcha_offers_refresh_token_route(hass, aioclient_mock) -> None:
    _login_page(aioclient_mock, CAPTCHA_HTML)

    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}
    )

    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "captcha"
    assert "refresh_token" in result["menu_options"]


async def test_refresh_token_flow_creates_entry(hass, aioclient_mock) -> None:
    _login_page(aioclient_mock, CAPTCHA_HTML)
    aioclient_mock.post(TOKEN, json=token_payload(jwt("auth0|user-2"), "RT-rotated"))
    _locations(aioclient_mock)

    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "refresh_token"}
    )
    assert result["step_id"] == "refresh_token"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_REFRESH_TOKEN: "RT-pasted"}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_EMAIL: "",
        CONF_PASSWORD: "",
        CONF_REFRESH_TOKEN: "RT-rotated",
        CONF_LOCATION_ID: 7,
    }
    assert result["result"].unique_id == "auth0|user-2"


async def test_refresh_token_invalid_shows_error(hass, aioclient_mock) -> None:
    _login_page(aioclient_mock, CAPTCHA_HTML)
    aioclient_mock.post(TOKEN, status=403, json={"error": "invalid_grant"})

    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "refresh_token"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_REFRESH_TOKEN: "RT-bad"}
    )

    assert result["errors"] == {"base": "invalid_auth"}


async def test_single_location_skips_location_step(hass, aioclient_mock) -> None:
    mock_login(aioclient_mock, access=jwt("auth0|user-1"))
    _locations(aioclient_mock, {"id": 7, "name": "Thuis"}, {"id": 8, "name": "Tweede verblijf"})

    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "location"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_LOCATION_ID: "8"}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_LOCATION_ID] == 8


async def test_duplicate_account_aborts(hass, aioclient_mock) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id="auth0|user-1", data={}).add_to_hass(hass)
    mock_login(aioclient_mock, access=jwt("auth0|user-1"))
    _locations(aioclient_mock)

    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_updates_entry(hass, aioclient_mock) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="auth0|user-1",
        data={
            CONF_EMAIL: EMAIL,
            CONF_PASSWORD: "old",
            CONF_REFRESH_TOKEN: "RT-old",
            CONF_LOCATION_ID: 7,
        },
    )
    entry.add_to_hass(hass)
    mock_login(aioclient_mock, access=jwt("auth0|user-1"), refresh="RT-new")

    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data == {
        CONF_EMAIL: EMAIL,
        CONF_PASSWORD: PASSWORD,
        CONF_REFRESH_TOKEN: "RT-new",
        CONF_LOCATION_ID: 7,
    }


async def test_reauth_with_other_account_aborts(hass, aioclient_mock) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="auth0|user-1",
        data={
            CONF_EMAIL: EMAIL,
            CONF_PASSWORD: "old",
            CONF_REFRESH_TOKEN: "RT-old",
            CONF_LOCATION_ID: 7,
        },
    )
    entry.add_to_hass(hass)
    mock_login(aioclient_mock, access=jwt("auth0|someone-else"))

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: "other@example.invalid", CONF_PASSWORD: PASSWORD}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert entry.data[CONF_PASSWORD] == "old"


async def test_unknown_login_response_offers_refresh_token_route(hass, aioclient_mock) -> None:
    """If Dots changes its login pages, the token route must still be reachable."""
    aioclient_mock.get(
        AUTHORIZE_RE, status=302, headers={"Location": "/u/login/identifier?state=ST9"}
    )
    aioclient_mock.get(
        "https://auth.dotsenergy.be/u/login/identifier?state=ST9",
        text="<form>new page</form>",
        headers={"Content-Type": "text/html"},
    )

    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}
    )

    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "login_unknown"
    assert result["menu_options"] == ["refresh_token", "user"]


async def test_reauth_unknown_response_offers_refresh_token_route(hass, aioclient_mock) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="auth0|user-1",
        data={
            CONF_EMAIL: EMAIL,
            CONF_PASSWORD: "old",
            CONF_REFRESH_TOKEN: "RT-old",
            CONF_LOCATION_ID: 7,
        },
    )
    entry.add_to_hass(hass)
    aioclient_mock.get(AUTHORIZE_RE, status=302, headers={"Location": "/somewhere-new"})
    aioclient_mock.get("https://auth.dotsenergy.be/somewhere-new", text="<html></html>")

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}
    )

    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["refresh_token", "reauth_confirm"]

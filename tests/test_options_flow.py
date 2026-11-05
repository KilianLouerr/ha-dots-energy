"""Options flow: choosing the sources of the forecast."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dots_energy.const import (
    CONF_LOCATION_ID,
    CONF_REFRESH_TOKEN,
    DOMAIN,
    OPT_AFNAME,
    OPT_CAPACITEIT,
    OPT_GESTUURD,
    OPT_INJECTIE,
    OPT_PRIJS,
    OPT_PRIJS_INCL_BTW,
)

from .test_entities import EMAIL, PASSWORD, _mock_api

SOURCES = {
    OPT_AFNAME: "sensor.my_grid_import",
    OPT_INJECTIE: "sensor.my_grid_export",
    OPT_CAPACITEIT: "sensor.my_capacity_average",
    OPT_GESTUURD: ["sensor.my_steered_in", "sensor.my_steered_out"],
    OPT_PRIJS: "sensor.my_day_ahead_price",
    OPT_PRIJS_INCL_BTW: True,
}


@pytest.fixture
async def entry(hass, aioclient_mock, freezer):
    freezer.move_to("2026-03-30 12:00:00+02:00")
    _mock_api(aioclient_mock)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="auth0|user-1",
        data={
            CONF_EMAIL: EMAIL,
            CONF_PASSWORD: PASSWORD,
            CONF_REFRESH_TOKEN: "RT-0",
            CONF_LOCATION_ID: 7,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_options_flow_saves_sources(hass, entry) -> None:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(result["flow_id"], SOURCES)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert dict(entry.options) == SOURCES


async def test_options_flow_all_optional(hass, entry) -> None:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options.get(OPT_AFNAME) is None


async def test_options_change_reloads_entry(hass, entry) -> None:
    with patch.object(hass.config_entries, "async_reload", AsyncMock(return_value=True)) as reload:
        result = await hass.config_entries.options.async_init(entry.entry_id)
        await hass.config_entries.options.async_configure(result["flow_id"], SOURCES)
        await hass.async_block_till_done()
    reload.assert_called_once_with(entry.entry_id)


async def test_token_rotation_does_not_reload_entry(hass, entry) -> None:
    with patch.object(hass.config_entries, "async_reload", AsyncMock(return_value=True)) as reload:
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_REFRESH_TOKEN: "RT-rotated"}
        )
        await hass.async_block_till_done()
    reload.assert_not_called()

"""Forecast coordinator: hourly recompute, stored prices, snapshots."""

from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.dots_energy.const import (
    CONF_LOCATION_ID,
    CONF_REFRESH_TOKEN,
    DOMAIN,
    OPT_AFNAME,
    OPT_PRIJS,
    OPT_PRIJS_INCL_BTW,
)
from custom_components.dots_energy.diagnostics import async_get_config_entry_diagnostics

from .test_entities import EMAIL, PASSWORD, _mock_api

IMPORT = "sensor.my_grid_import"
PRICE = "sensor.my_day_ahead_price"
NOW = "2026-03-20 12:30:00+01:00"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    """Start the recorder before hass, as recorder tests require."""
    yield


async def _import_hourly(hass, entity_id: str, start, end, kwh: float = 1.0) -> None:
    metadata = StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=None,
        source="recorder",
        statistic_id=entity_id,
        unit_class="energy",
        unit_of_measurement="kWh",
    )
    rows, total, hour = [], 0.0, start
    while hour < end:
        rows.append(StatisticData(start=hour, state=total, sum=total))
        total += kwh
        hour += timedelta(hours=1)
    async_import_statistics(hass, metadata, rows)
    await async_wait_recording_done(hass)


async def _setup(hass, aioclient_mock, freezer, options, now: str = NOW) -> MockConfigEntry:
    await hass.config.async_update(time_zone="Europe/Brussels")
    freezer.move_to(now)
    _mock_api(aioclient_mock)
    tz = dt_util.get_default_time_zone()
    start = dt_util.parse_datetime("2026-01-31 23:00:00").replace(tzinfo=tz)
    end = dt_util.now().replace(minute=0, second=0, microsecond=0)
    await _import_hourly(hass, IMPORT, start, end)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="auth0|user-1",
        data={
            CONF_EMAIL: EMAIL,
            CONF_PASSWORD: PASSWORD,
            CONF_REFRESH_TOKEN: "RT-0",
            CONF_LOCATION_ID: 7,
        },
        options=options,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_forecast_none_without_afname_option(hass, aioclient_mock, freezer) -> None:
    entry = await _setup(hass, aioclient_mock, freezer, {})
    assert entry.runtime_data.forecast.data is None


async def test_forecast_computes_so_far_and_expected(hass, aioclient_mock, freezer) -> None:
    entry = await _setup(hass, aioclient_mock, freezer, {OPT_AFNAME: IMPORT})
    data = entry.runtime_data.forecast.data
    assert data.month == "2026-03"
    assert data.previous_month == "2026-02"
    f = data.forecast
    assert f.day == 20
    assert f.days_in_month == 31
    assert f.so_far.total > 0
    assert f.expected.total > f.so_far.total
    assert len(f.daily) == 20
    assert f.daily[-1] == (20, f.so_far.total)
    assert len(data.previous_daily) == 28
    assert data.quality == "ruw"
    assert "marktprijs" in f.missing


async def test_prices_recorded_hourly_into_store(
    hass, aioclient_mock, freezer, hass_storage
) -> None:
    entry = await _setup(
        hass,
        aioclient_mock,
        freezer,
        {OPT_AFNAME: IMPORT, OPT_PRIJS: PRICE, OPT_PRIJS_INCL_BTW: True},
    )
    hass.states.async_set(PRICE, "0.106")
    next_run = dt_util.now().replace(minute=12, second=0, microsecond=0) + timedelta(hours=1)
    freezer.move_to(next_run)
    async_fire_time_changed(hass, next_run)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data.forecast
    await coordinator.async_flush()
    prices = hass_storage[f"{DOMAIN}.{entry.entry_id}.forecast"]["data"]["prices"]
    hour = next_run.replace(minute=0).astimezone(dt_util.UTC).isoformat()
    assert prices[hour] == "0.1"


async def test_snapshot_once_per_day_and_last_overwritten(hass, aioclient_mock, freezer) -> None:
    entry = await _setup(hass, aioclient_mock, freezer, {OPT_AFNAME: IMPORT})
    coordinator = entry.runtime_data.forecast
    snapshots = coordinator.stored["snapshots"]["2026-03"]
    first_day = dict(snapshots["2026-03-20"])
    first_last = dict(snapshots["laatste"])
    freezer.tick(timedelta(hours=2))
    await coordinator.async_refresh()
    snapshots = coordinator.stored["snapshots"]["2026-03"]
    assert snapshots["2026-03-20"] == first_day
    assert snapshots["laatste"]["berekend_op"] != first_last["berekend_op"]


async def test_month_rollover_starts_new_series(hass, aioclient_mock, freezer) -> None:
    entry = await _setup(
        hass, aioclient_mock, freezer, {OPT_AFNAME: IMPORT}, now="2026-03-31 23:40:00+02:00"
    )
    coordinator = entry.runtime_data.forecast
    freezer.move_to("2026-04-01 00:30:00+02:00")
    await coordinator.async_refresh()
    data = coordinator.data
    assert data.month == "2026-04"
    assert data.previous_month == "2026-03"
    assert data.forecast.daily == ((1, data.forecast.so_far.total),)
    assert data.forecast.so_far.total == 0
    assert "2026-04" in coordinator.stored["snapshots"]


async def test_refits_when_invoices_update(hass, aioclient_mock, freezer) -> None:
    entry = await _setup(hass, aioclient_mock, freezer, {OPT_AFNAME: IMPORT})
    runtime = entry.runtime_data
    with patch.object(runtime.forecast, "async_request_refresh", AsyncMock()) as refresh:
        runtime.invoices.async_update_listeners()
        await hass.async_block_till_done()
        refresh.assert_not_called()
        freezer.tick(timedelta(minutes=5))
        await runtime.invoices.async_refresh()
        await hass.async_block_till_done()
    refresh.assert_called()


async def test_unavailable_price_sensor_marks_ruw(hass, aioclient_mock, freezer) -> None:
    entry = await _setup(hass, aioclient_mock, freezer, {OPT_AFNAME: IMPORT, OPT_PRIJS: PRICE})
    hass.states.async_set(PRICE, "unavailable")
    await entry.runtime_data.forecast.async_refresh()
    data = entry.runtime_data.forecast.data
    assert data.quality == "ruw"
    assert "marktprijs" in data.forecast.missing


async def test_diagnostics_contains_forecast_without_entity_ids(
    hass, aioclient_mock, freezer
) -> None:
    entry = await _setup(hass, aioclient_mock, freezer, {OPT_AFNAME: IMPORT, OPT_PRIJS: PRICE})
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    dumped = json.dumps(diagnostics, default=str)
    assert diagnostics["forecast"]["month"] == "2026-03"
    assert IMPORT not in dumped
    assert PRICE not in dumped

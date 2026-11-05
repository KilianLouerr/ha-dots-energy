"""Forecast coordinator: hourly recompute, stored prices, snapshots."""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.db_schema import StatisticsShortTerm
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


async def _setup(
    hass, aioclient_mock, freezer, options, now: str = NOW, entry_id: str | None = None
) -> MockConfigEntry:
    await hass.config.async_update(time_zone="Europe/Brussels")
    freezer.move_to(now)
    _mock_api(aioclient_mock)
    tz = dt_util.get_default_time_zone()
    start = dt_util.parse_datetime("2025-12-31 23:00:00").replace(tzinfo=tz)
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
        **({"entry_id": entry_id} if entry_id else {}),
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
    quarter = next_run.replace(minute=0).astimezone(dt_util.UTC)
    assert entry.runtime_data.forecast.price_at(quarter) == Decimal("0.1")
    await entry.runtime_data.forecast.async_flush()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.forecast.price_at(quarter) == Decimal("0.1")


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
    freezer.move_to("2026-04-01 00:10:00+02:00")
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


async def test_forecast_sensors_state_and_attributes(hass, aioclient_mock, freezer) -> None:
    await _setup(hass, aioclient_mock, freezer, {OPT_AFNAME: IMPORT})
    expected = hass.states.get("sensor.dots_energy_verwachte_factuur")
    so_far = hass.states.get("sensor.dots_energy_factuur_tot_nu_toe")
    assert expected.attributes["unit_of_measurement"] == "EUR"
    assert expected.attributes["maand"] == "2026-03"
    assert round(sum(expected.attributes["categorieen"].values()), 2) == float(expected.state)
    assert round(sum(r["bedrag"] for r in expected.attributes["regels"]), 2) == float(
        expected.state
    )
    assert expected.attributes["vorige_factuur"] == 88.38
    assert so_far.attributes["dag"] == 20
    assert so_far.attributes["dagen_in_maand"] == 31
    assert so_far.attributes["dagreeks"][-1] == {"dag": 20, "totaal": float(so_far.state)}
    assert so_far.attributes["vorige_maand"] == "2026-02"
    assert len(so_far.attributes["dagreeks_vorige_maand"]) == 28
    assert round(sum(so_far.attributes["categorieen"].values()), 2) == float(so_far.state)


async def test_forecast_sensors_ruw_flag(hass, aioclient_mock, freezer) -> None:
    await _setup(hass, aioclient_mock, freezer, {OPT_AFNAME: IMPORT})
    for entity_id in (
        "sensor.dots_energy_verwachte_factuur",
        "sensor.dots_energy_factuur_tot_nu_toe",
    ):
        state = hass.states.get(entity_id)
        assert state.attributes["kwaliteit"] == "ruw"
        assert "marktprijs" in state.attributes["ontbreekt"]


async def test_closed_hour_price_is_time_weighted_average(
    hass, aioclient_mock, freezer, hass_storage
) -> None:
    entry = await _setup(hass, aioclient_mock, freezer, {OPT_AFNAME: IMPORT, OPT_PRIJS: PRICE})
    hour = dt_util.now().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    freezer.move_to(hour)
    hass.states.async_set(PRICE, "0.10")
    await hass.async_block_till_done()
    freezer.move_to(hour + timedelta(minutes=30))
    hass.states.async_set(PRICE, "0.20")
    await hass.async_block_till_done()
    run = hour + timedelta(hours=1, minutes=12)
    freezer.move_to(run)
    async_fire_time_changed(hass, run)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data.forecast
    utc = hour.astimezone(dt_util.UTC)
    assert coordinator.price_at(utc) == Decimal("0.1")
    assert coordinator.price_at(utc + timedelta(minutes=15)) == Decimal("0.1")
    assert coordinator.price_at(utc + timedelta(minutes=30)) == Decimal("0.2")


def _short_term(hass, entity_id, sums, start):
    metadata = StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=None,
        source="recorder",
        statistic_id=entity_id,
        unit_class="energy",
        unit_of_measurement="kWh",
    )
    rows = [
        StatisticData(start=start + timedelta(minutes=5 * i), state=0, sum=value)
        for i, value in enumerate(sums)
    ]
    get_instance(hass).async_import_statistics(metadata, rows, StatisticsShortTerm)


async def test_quarters_recorded_from_five_minute_statistics(hass, aioclient_mock, freezer) -> None:
    entry = await _setup(hass, aioclient_mock, freezer, {OPT_AFNAME: IMPORT})
    start = dt_util.now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    before = start - timedelta(minutes=5)
    _short_term(hass, IMPORT, [0, 0, 0, 0, 1.0, 1.5, 2.0], before)
    await async_wait_recording_done(hass)
    coordinator = entry.runtime_data.forecast
    await coordinator.async_refresh()
    utc = start.astimezone(dt_util.UTC)
    assert coordinator.quarter_at(IMPORT, utc) == Decimal(0)
    assert coordinator.quarter_at(IMPORT, utc + timedelta(minutes=15)) == Decimal(2)


async def test_legacy_hourly_prices_are_expanded_to_quarters(
    hass, aioclient_mock, freezer, hass_storage
) -> None:
    hour = "2026-03-20T09:00:00+00:00"
    hass_storage[f"{DOMAIN}.legacy.forecast"] = {
        "version": 1,
        "key": f"{DOMAIN}.legacy.forecast",
        "data": {"prices": {hour: "0.2", "2025-12-01T09:00:00+00:00": "0.3"}},
    }
    entry = await _setup(hass, aioclient_mock, freezer, {OPT_AFNAME: IMPORT}, entry_id="legacy")
    coordinator = entry.runtime_data.forecast
    base = dt_util.parse_datetime(hour)
    assert coordinator.price_at(base + timedelta(minutes=45)) == Decimal("0.2")
    assert coordinator.price_at(dt_util.parse_datetime("2025-12-01T09:00:00+00:00")) is None


async def test_complete_months_are_kept_as_totals(hass, aioclient_mock, freezer) -> None:
    entry = await _setup(hass, aioclient_mock, freezer, {OPT_AFNAME: IMPORT})
    measured = entry.runtime_data.forecast.stored["measured"]
    assert "2026-01" in measured
    assert "2026-02" in measured


def _legacy_store(hass_storage, entry_id: str, prices: dict[str, str], **extra) -> None:
    hass_storage[f"{DOMAIN}.{entry_id}.forecast"] = {
        "version": 1,
        "key": f"{DOMAIN}.{entry_id}.forecast",
        "data": {"prices": prices, **extra},
    }


async def test_older_months_keep_their_market_prices(
    hass, aioclient_mock, freezer, hass_storage
) -> None:
    start = dt_util.parse_datetime("2026-01-01T00:00:00+01:00")
    prices = {
        (start + timedelta(hours=h)).astimezone(dt_util.UTC).isoformat(): "0.1"
        for h in range(31 * 24)
    }
    _legacy_store(hass_storage, "older", prices)
    entry = await _setup(
        hass, aioclient_mock, freezer, {OPT_AFNAME: IMPORT, OPT_PRIJS: PRICE}, entry_id="older"
    )
    january = entry.runtime_data.forecast.stored["measured"]["2026-01"]
    assert Decimal(january["priced_in"][0]) > 700


async def test_changed_sources_drop_month_totals(hass, aioclient_mock, freezer) -> None:
    entry = await _setup(hass, aioclient_mock, freezer, {OPT_AFNAME: IMPORT})
    coordinator = entry.runtime_data.forecast
    assert Decimal(coordinator.stored["measured"]["2026-01"]["kwh_in"]) > 700
    hass.config_entries.async_update_entry(entry, options={OPT_AFNAME: "sensor.my_other_meter"})
    await coordinator.async_refresh()
    assert Decimal(coordinator.stored["measured"]["2026-01"]["kwh_in"]) == 0


async def test_published_quarter_price_wins_over_state_history(
    hass, aioclient_mock, freezer
) -> None:
    entry = await _setup(hass, aioclient_mock, freezer, {OPT_AFNAME: IMPORT, OPT_PRIJS: PRICE})
    now = dt_util.utcnow()
    quarter = now.replace(minute=now.minute - now.minute % 15, second=0, microsecond=0)
    raw = [{"start": quarter, "end": quarter + timedelta(minutes=15), "value": 0.10}]
    hass.states.async_set(PRICE, "0.30", {"raw_today": raw})
    await hass.async_block_till_done()
    freezer.tick(timedelta(hours=1))
    await entry.runtime_data.forecast.async_refresh()
    assert entry.runtime_data.forecast.price_at(quarter) == Decimal("0.1")


async def test_legacy_hourly_prices_are_fetched_again_per_quarter(
    hass, aioclient_mock, freezer, hass_storage
) -> None:
    await hass.config.async_update(time_zone="Europe/Brussels")
    earlier = dt_util.parse_datetime("2026-03-17T10:00:00+01:00")
    freezer.move_to(earlier)
    hass.states.async_set(PRICE, "0.40")
    await hass.async_block_till_done()
    freezer.move_to(earlier + timedelta(minutes=30))
    hass.states.async_set(PRICE, "0.20")
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)
    _legacy_store(
        hass_storage,
        "relegacy",
        {earlier.astimezone(dt_util.UTC).isoformat(): "0.3"},
        price_backfill=PRICE,
    )
    entry = await _setup(
        hass, aioclient_mock, freezer, {OPT_AFNAME: IMPORT, OPT_PRIJS: PRICE}, entry_id="relegacy"
    )
    coordinator = entry.runtime_data.forecast
    assert coordinator.price_at(earlier) == Decimal("0.4")
    assert coordinator.price_at(earlier + timedelta(minutes=30)) == Decimal("0.2")

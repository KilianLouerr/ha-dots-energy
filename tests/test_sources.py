"""Reading hourly statistics, prices and capacity from Home Assistant."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.db_schema import StatisticsShortTerm
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.dots_energy.sources import (
    async_hourly_changes,
    async_price_history,
    async_quarter_changes,
    capacity_kw,
    current_price,
    raw_prices,
)

D = Decimal
VAT = D("0.06")
START = datetime(2026, 1, 5, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    """Start the recorder before hass, as recorder tests require."""
    yield


async def test_hourly_changes_reads_statistics(recorder_mock, hass) -> None:
    metadata = StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=None,
        source="recorder",
        statistic_id="sensor.my_grid_import",
        unit_class="energy",
        unit_of_measurement="kWh",
    )
    rows = [
        StatisticData(start=START + timedelta(hours=i), state=10 + i, sum=float(i * (i + 1) / 2))
        for i in range(4)
    ]
    async_import_statistics(hass, metadata, rows)
    await async_wait_recording_done(hass)
    series = await async_hourly_changes(
        hass, "sensor.my_grid_import", START, START + timedelta(hours=4)
    )
    assert series[START + timedelta(hours=1)] == D(1)
    assert series[START + timedelta(hours=3)] == D(3)
    assert all(h.tzinfo == UTC for h in series)


async def test_hourly_changes_without_statistics_is_empty(recorder_mock, hass) -> None:
    series = await async_hourly_changes(hass, "sensor.nothing", START, START + timedelta(hours=4))
    assert series == {}


def _state(hass, value, attributes=None):
    hass.states.async_set("sensor.my_day_ahead_price", value, attributes or {})


async def test_current_price_uses_raw_today_and_tomorrow(hass) -> None:
    now = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    q = timedelta(minutes=15)
    raw_today = [
        {"start": now.isoformat(), "end": (now + q).isoformat(), "value": 0.10},
        {"start": now + q, "end": now + 2 * q, "value": 0.20},
    ]
    tomorrow = now + timedelta(hours=24)
    raw_tomorrow = [{"start": tomorrow.isoformat(), "end": tomorrow + q, "value": 0.30}]
    _state(hass, "0.15", {"raw_today": raw_today, "raw_tomorrow": raw_tomorrow})
    prices = current_price(hass, "sensor.my_day_ahead_price", False, VAT)
    assert prices[now] == D("0.1")
    assert prices[now + q] == D("0.2")
    assert prices[tomorrow] == D("0.3")


async def test_current_price_hourly_raw_entry_covers_four_quarters(hass) -> None:
    hour = dt_util.utcnow().replace(minute=0, second=0, microsecond=0) + timedelta(hours=2)
    raw = [{"start": hour, "end": hour + timedelta(hours=1), "value": 0.12}]
    _state(hass, "unknown", {"raw_today": raw})
    prices = current_price(hass, "sensor.my_day_ahead_price", False, VAT)
    assert [prices[hour + timedelta(minutes=15 * i)] for i in range(4)] == [D("0.12")] * 4


async def test_current_price_from_state_only(hass) -> None:
    _state(hass, "0.25")
    prices = current_price(hass, "sensor.my_day_ahead_price", False, VAT)
    now = dt_util.utcnow()
    quarter = now.replace(minute=now.minute - now.minute % 15, second=0, microsecond=0)
    assert prices == {quarter: D("0.25")}


async def test_current_price_strips_vat(hass) -> None:
    _state(hass, "0.106")
    prices = current_price(hass, "sensor.my_day_ahead_price", True, VAT)
    assert list(prices.values()) == [D("0.1")]


async def test_current_price_unavailable_is_empty(hass) -> None:
    _state(hass, "unavailable")
    assert current_price(hass, "sensor.my_day_ahead_price", False, VAT) == {}
    assert current_price(hass, "sensor.not_there", False, VAT) == {}


async def test_price_history_hourly_average(recorder_mock, hass, freezer) -> None:
    t0 = dt_util.utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(hours=3)
    freezer.move_to(t0)
    _state(hass, "0.10")
    await hass.async_block_till_done()
    freezer.move_to(t0 + timedelta(minutes=30))
    _state(hass, "0.20")
    await hass.async_block_till_done()
    freezer.move_to(t0 + timedelta(hours=1))
    _state(hass, "0.40")
    await hass.async_block_till_done()
    freezer.move_to(t0 + timedelta(hours=2, minutes=5))
    await async_wait_recording_done(hass)
    prices = await async_price_history(
        hass, "sensor.my_day_ahead_price", t0, t0 + timedelta(hours=2), False, VAT
    )
    q = timedelta(minutes=15)
    assert [prices[t0 + q * i] for i in range(4)] == [D("0.1"), D("0.1"), D("0.2"), D("0.2")]
    assert prices[t0 + timedelta(hours=1)] == D("0.4")


async def test_capacity_kw(hass) -> None:
    hass.states.async_set("sensor.my_capacity_average", "4.25")
    assert capacity_kw(hass, "sensor.my_capacity_average") == D("4.25")
    hass.states.async_set("sensor.my_capacity_average", "unknown")
    assert capacity_kw(hass, "sensor.my_capacity_average") is None
    assert capacity_kw(hass, None) is None


async def test_hourly_changes_converts_wh_to_kwh(recorder_mock, hass) -> None:
    metadata = StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=None,
        source="recorder",
        statistic_id="sensor.my_grid_import_wh",
        unit_class="energy",
        unit_of_measurement="Wh",
    )
    rows = [
        StatisticData(start=START + timedelta(hours=i), state=0, sum=float(i * 1500))
        for i in range(3)
    ]
    async_import_statistics(hass, metadata, rows)
    await async_wait_recording_done(hass)
    series = await async_hourly_changes(
        hass, "sensor.my_grid_import_wh", START, START + timedelta(hours=3)
    )
    assert series[START + timedelta(hours=2)] == D("1.5")


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


async def test_quarter_changes_sum_five_minute_rows(recorder_mock, hass) -> None:
    before = START - timedelta(minutes=5)
    _short_term(hass, "sensor.my_grid_import", [0, 0.1, 0.2, 0.3, 1.3, 2.3, 3.3], before)
    await async_wait_recording_done(hass)
    series = await async_quarter_changes(
        hass, "sensor.my_grid_import", START, START + timedelta(minutes=30)
    )
    assert series[START] == D("0.3")
    assert series[START + timedelta(minutes=15)] == D("3.0")


async def test_quarter_with_missing_rows_is_absent(recorder_mock, hass) -> None:
    before = START - timedelta(minutes=5)
    _short_term(hass, "sensor.my_grid_import", [0, 0.1, 0.2], before)
    await async_wait_recording_done(hass)
    series = await async_quarter_changes(
        hass, "sensor.my_grid_import", START, START + timedelta(minutes=15)
    )
    assert START not in series


async def test_first_five_minute_row_is_not_counted_as_cumulative(recorder_mock, hass) -> None:
    """A window starting before the oldest row must not turn the meter total into a quarter."""
    _short_term(
        hass, "sensor.my_grid_import", [5000, 5000.1, 5000.2, 5000.3, 5000.4, 5000.5], START
    )
    await async_wait_recording_done(hass)
    series = await async_quarter_changes(
        hass, "sensor.my_grid_import", START - timedelta(hours=1), START + timedelta(minutes=30)
    )
    assert START not in series
    assert series[START + timedelta(minutes=15)] == D("0.3")


async def test_raw_prices_separate_from_state(hass) -> None:
    now = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    raw = [{"start": now, "end": now + timedelta(minutes=15), "value": 0.10}]
    _state(hass, "0.30", {"raw_today": raw})
    assert raw_prices(hass, "sensor.my_day_ahead_price", False, VAT) == {now: D("0.1")}

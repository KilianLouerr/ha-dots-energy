"""Read the forecast inputs from Home Assistant: statistics, prices and capacity."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from homeassistant.components.recorder import get_instance, history
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util

from .projection import HourlySeries

HOUR = timedelta(hours=1)


def _decimal(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except InvalidOperation, TypeError, ValueError:
        return None
    return number if number.is_finite() else None


def _hour(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, UTC)
    if isinstance(value, str):
        return dt_util.parse_datetime(value)
    return None


async def async_hourly_changes(
    hass: HomeAssistant, entity_id: str, start: datetime, end: datetime
) -> HourlySeries:
    """Hourly 'change' of a statistic between start and end (UTC hours)."""
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, start, end, {entity_id}, "hour", None, {"change"}
    )
    values: dict[datetime, Decimal] = {}
    for row in rows.get(entity_id, []):
        start_time = _as_datetime(row.get("start"))
        change = _decimal(row.get("change")) if row.get("change") is not None else None
        if start_time is not None and change is not None:
            values[_hour(start_time)] = change
    return HourlySeries(values=values)


def _net(price: Decimal, incl_vat: bool, vat_rate: Decimal) -> Decimal:
    return price / (1 + vat_rate) if incl_vat else price


def _raw_prices(entries: Iterable[Any]) -> dict[datetime, list[Decimal]]:
    by_hour: dict[datetime, list[Decimal]] = {}
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        start = _as_datetime(entry.get("start"))
        value = _decimal(entry.get("value"))
        if start is not None and value is not None:
            by_hour.setdefault(_hour(start), []).append(value)
    return by_hour


def current_price(
    hass: HomeAssistant, entity_id: str, incl_vat: bool, vat_rate: Decimal
) -> dict[datetime, Decimal]:
    """Known hourly prices (EUR/kWh, without VAT) from the price sensor right now."""
    state = hass.states.get(entity_id)
    if state is None:
        return {}
    by_hour: dict[datetime, list[Decimal]] = {}
    for key in ("raw_today", "raw_tomorrow"):
        for hour, values in _raw_prices(state.attributes.get(key)).items():
            by_hour.setdefault(hour, []).extend(values)
    prices = {h: _net(sum(v, Decimal(0)) / len(v), incl_vat, vat_rate) for h, v in by_hour.items()}
    value = _decimal(state.state)
    if value is not None:
        prices.setdefault(_hour(dt_util.utcnow()), _net(value, incl_vat, vat_rate))
    return prices


def _hourly_average(states: list[State], start: datetime, end: datetime) -> dict[datetime, Decimal]:
    """Time-weighted average per hour of a numeric state history."""
    weighted: dict[datetime, Decimal] = {}
    seconds: dict[datetime, Decimal] = {}
    for index, state in enumerate(states):
        value = _decimal(state.state)
        if value is None:
            continue
        begin = max(state.last_changed, start)
        stop = states[index + 1].last_changed if index + 1 < len(states) else end
        stop = min(stop, end)
        while begin < stop:
            hour = _hour(begin)
            part_end = min(hour + HOUR, stop)
            span = Decimal(int((part_end - begin).total_seconds()))
            weighted[hour] = weighted.get(hour, Decimal(0)) + value * span
            seconds[hour] = seconds.get(hour, Decimal(0)) + span
            begin = part_end
    return {h: weighted[h] / seconds[h] for h in weighted if seconds[h] > 0}


async def async_price_history(
    hass: HomeAssistant,
    entity_id: str,
    start: datetime,
    end: datetime,
    incl_vat: bool,
    vat_rate: Decimal,
) -> dict[datetime, Decimal]:
    """Hourly prices (without VAT) reconstructed from the recorder's state history."""
    # Start an hour early so a state set exactly at `start` is part of the answer.
    result = await get_instance(hass).async_add_executor_job(
        history.state_changes_during_period, hass, start - HOUR, end, entity_id, True
    )
    averages = _hourly_average(result.get(entity_id, []), start, end)
    return {h: _net(v, incl_vat, vat_rate) for h, v in averages.items()}


def capacity_kw(hass: HomeAssistant, entity_id: str | None) -> Decimal | None:
    """Current value of the 12-month average peak entity, if numeric."""
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    return _decimal(state.state) if state is not None else None

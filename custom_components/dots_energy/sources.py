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

from .projection import SLOT

HOUR = timedelta(hours=1)
FIVE_MINUTES_PER_SLOT = 3
KWH_PRECISION = Decimal("0.000001")


def _decimal(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except InvalidOperation, TypeError, ValueError:
        return None
    return number if number.is_finite() else None


def slot_floor(moment: datetime) -> datetime:
    """UTC start of the quarter hour containing `moment`."""
    utc = moment.astimezone(UTC)
    return utc.replace(minute=utc.minute - utc.minute % 15, second=0, microsecond=0)


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, UTC)
    if isinstance(value, str):
        return dt_util.parse_datetime(value)
    return None


async def _changes(
    hass: HomeAssistant, entity_id: str, start: datetime, end: datetime, period: str
) -> list[tuple[datetime, Decimal]]:
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        end,
        {entity_id},
        period,
        {"energy": "kWh"},
        {"change"},
    )
    result = []
    for row in rows.get(entity_id, []):
        start_time = _as_datetime(row.get("start"))
        change = _decimal(row.get("change")) if row.get("change") is not None else None
        if start_time is not None and change is not None:
            # Statistics are floats; a micro-kWh is far below any meter's resolution.
            result.append((start_time.astimezone(UTC), change.quantize(KWH_PRECISION)))
    return result


async def async_hourly_changes(
    hass: HomeAssistant, entity_id: str, start: datetime, end: datetime
) -> dict[datetime, Decimal]:
    """kWh per hour (UTC hour start) from the long-term statistics."""
    return dict(await _changes(hass, entity_id, start, end, "hour"))


async def async_quarter_changes(
    hass: HomeAssistant, entity_id: str, start: datetime, end: datetime
) -> dict[datetime, Decimal]:
    """kWh per quarter hour from the 5-minute statistics (kept about 10 days).

    A quarter is only returned when all three of its 5-minute rows exist.
    """
    sums: dict[datetime, Decimal] = {}
    counts: dict[datetime, int] = {}
    for moment, change in await _changes(hass, entity_id, start, end, "5minute"):
        slot = slot_floor(moment)
        sums[slot] = sums.get(slot, Decimal(0)) + change
        counts[slot] = counts.get(slot, 0) + 1
    return {q: v for q, v in sums.items() if counts[q] == FIVE_MINUTES_PER_SLOT}


def _net(price: Decimal, incl_vat: bool, vat_rate: Decimal) -> Decimal:
    return price / (1 + vat_rate) if incl_vat else price


def _raw_prices(entries: Iterable[Any]) -> dict[datetime, Decimal]:
    """Price per quarter from Nord Pool style raw entries (quarter or hour long)."""
    prices: dict[datetime, Decimal] = {}
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        start = _as_datetime(entry.get("start"))
        end = _as_datetime(entry.get("end"))
        value = _decimal(entry.get("value"))
        if start is None or value is None:
            continue
        slot = slot_floor(start)
        stop = end.astimezone(UTC) if end is not None else slot + SLOT
        while True:
            prices[slot] = value
            slot += SLOT
            if slot >= stop:
                break
    return prices


def current_price(
    hass: HomeAssistant, entity_id: str, incl_vat: bool, vat_rate: Decimal
) -> dict[datetime, Decimal]:
    """Known prices per quarter hour (EUR/kWh, without VAT) from the price sensor now."""
    state = hass.states.get(entity_id)
    if state is None:
        return {}
    raw: dict[datetime, Decimal] = {}
    for key in ("raw_today", "raw_tomorrow"):
        raw.update(_raw_prices(state.attributes.get(key)))
    prices = {q: _net(p, incl_vat, vat_rate) for q, p in raw.items()}
    value = _decimal(state.state)
    if value is not None:
        prices.setdefault(slot_floor(dt_util.utcnow()), _net(value, incl_vat, vat_rate))
    return prices


def _slot_average(states: list[State], start: datetime, end: datetime) -> dict[datetime, Decimal]:
    """Time-weighted average per quarter hour of a numeric state history."""
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
            slot = slot_floor(begin)
            part_end = min(slot + SLOT, stop)
            span = Decimal(int((part_end - begin).total_seconds()))
            weighted[slot] = weighted.get(slot, Decimal(0)) + value * span
            seconds[slot] = seconds.get(slot, Decimal(0)) + span
            begin = part_end
    return {q: weighted[q] / seconds[q] for q in weighted if seconds[q] > 0}


async def async_price_history(
    hass: HomeAssistant,
    entity_id: str,
    start: datetime,
    end: datetime,
    incl_vat: bool,
    vat_rate: Decimal,
) -> dict[datetime, Decimal]:
    """Prices per quarter hour (without VAT) from the recorder's state history."""
    # Start an hour early so a state set exactly at `start` is part of the answer.
    result = await get_instance(hass).async_add_executor_job(
        history.state_changes_during_period, hass, start - HOUR, end, entity_id, True
    )
    averages = _slot_average(result.get(entity_id, []), start, end)
    return {q: _net(v, incl_vat, vat_rate) for q, v in averages.items()}


def capacity_kw(hass: HomeAssistant, entity_id: str | None) -> Decimal | None:
    """Current value of the 12-month average peak entity, if numeric."""
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    return _decimal(state.state) if state is not None else None

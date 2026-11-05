"""Turn quarter-hour series of the running month into 'so far' and 'expected' bills.

Pure Python. A slot is a quarter hour, keyed by its UTC start; months are
calendar months in the local time zone of `month_start`. Dots bills energy per
quarter hour, so usage and prices are matched per slot.
"""

from __future__ import annotations

import calendar
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from .tariff import Bill, FitParams, MonthUsage, TariffRules, price_month

SLOT = timedelta(minutes=15)
SLOTS_PER_HOUR = 4
SLOTS_PER_DAY = 96
MAX_SLOT_KWH = Decimal("12.5")  # 50 kWh per hour
BLEND_DAYS = Decimal(7)
ZERO = Decimal(0)


@dataclass(frozen=True)
class SlotSeries:
    """kWh per quarter hour (UTC start of the quarter); absent = not measured."""

    values: Mapping[datetime, Decimal]


def merge_quarters(
    hourly: Mapping[datetime, Decimal], quarters: Mapping[datetime, Decimal]
) -> dict[datetime, Decimal]:
    """Measured quarters where known; the rest of each hour from its hourly total.

    Quarters missing within an hour share what the measured quarters leave of the
    hourly total, so the hour still adds up.
    """
    merged = dict(quarters)
    for hour, total in hourly.items():
        slots = [hour + SLOT * index for index in range(SLOTS_PER_HOUR)]
        missing = [q for q in slots if q not in quarters]
        if not missing:
            continue
        rest = max(total - sum((quarters[q] for q in slots if q in quarters), ZERO), ZERO)
        for q in missing:
            merged[q] = rest / len(missing)
    return merged


@dataclass(frozen=True)
class MonthMeasuredDaily:
    """Average kWh per day of the previous month."""

    kwh_in_per_day: Decimal
    kwh_out_per_day: Decimal
    steered_per_day: Decimal | None


@dataclass(frozen=True)
class MonthContext:
    """Everything known about the month being forecast."""

    month_start: datetime
    now: datetime
    afname: SlotSeries
    injectie: SlotSeries | None
    gestuurd: SlotSeries | None
    prices: Mapping[datetime, Decimal]
    capacity_kw: Decimal | None
    previous: MonthMeasuredDaily | None


@dataclass(frozen=True)
class Forecast:
    """Result for the running month."""

    so_far: Bill
    expected: Bill
    elapsed_days: Decimal
    day: int
    days_in_month: int
    daily: tuple[tuple[int, Decimal], ...]
    missing: frozenset[str]


def _next_month(month_start: datetime) -> datetime:
    year, month = month_start.year, month_start.month
    return month_start.replace(year=year + month // 12, month=month % 12 + 1)


def _days_in_month(month_start: datetime) -> int:
    return calendar.monthrange(month_start.year, month_start.month)[1]


def month_slots(month_start: datetime) -> list[datetime]:
    """UTC starts of every quarter hour in the local calendar month (DST-correct)."""
    slot = month_start.astimezone(UTC)
    end = _next_month(month_start).astimezone(UTC)
    slots = []
    while slot < end:
        slots.append(slot)
        slot += SLOT
    return slots


def _local_days(month_start: datetime, until: datetime) -> Decimal:
    """Calendar days between the month start and `until`, in local wall time."""
    local = until.astimezone(month_start.tzinfo).replace(tzinfo=None)
    seconds = (local - month_start.replace(tzinfo=None)).total_seconds()
    return Decimal(int(seconds)) / Decimal(86400)


def _closed(ctx: MonthContext, until: datetime) -> list[datetime]:
    return [q for q in month_slots(ctx.month_start) if q + SLOT <= until]


def _time_of_day(slot: datetime, tz) -> tuple[int, int]:
    local = slot.astimezone(tz)
    return local.hour, local.minute


def _filled(
    series: SlotSeries | None, slots: list[datetime], tz
) -> tuple[dict[datetime, Decimal], int]:
    """Valid values; invalid or missing quarters filled with a same-time-of-day average."""
    if series is None:
        return {}, 0
    valid: dict[datetime, Decimal] = {}
    for q in slots:
        value = series.values.get(q)
        if value is not None and ZERO <= value <= MAX_SLOT_KWH:
            valid[q] = value
    by_time: dict[tuple[int, int], list[Decimal]] = {}
    for q, value in valid.items():
        by_time.setdefault(_time_of_day(q, tz), []).append(value)
    overall = sum(valid.values(), ZERO) / len(valid) if valid else ZERO
    result, invalid = dict(valid), 0
    for q in slots:
        if q in valid:
            continue
        invalid += 1
        same = by_time.get(_time_of_day(q, tz))
        result[q] = sum(same, ZERO) / len(same) if same else overall
    return result, invalid


def _priced(values: dict[datetime, Decimal], prices: Mapping[datetime, Decimal]):
    kwh, cost = ZERO, ZERO
    for h, value in values.items():
        price = prices.get(h)
        if price is not None:
            kwh += value
            cost += value * price
    return kwh, cost


def _measure(ctx: MonthContext, until: datetime):
    hours = _closed(ctx, until)
    tz = ctx.month_start.tzinfo
    afname, bad_in = _filled(ctx.afname, hours, tz)
    injectie, bad_out = _filled(ctx.injectie, hours, tz)
    gestuurd, bad_steer = _filled(ctx.gestuurd, hours, tz)
    invalid = {"afname": bad_in, "injectie": bad_out, "gestuurd": bad_steer}
    return hours, afname, injectie, gestuurd, invalid


def usage_until(ctx: MonthContext, until: datetime) -> tuple[MonthUsage, int]:
    """Usage of the closed quarters before `until`, and the number of filled quarters."""
    usage, invalid = usage_until_by_series(ctx, until)
    return usage, sum(invalid.values())


def usage_until_by_series(ctx: MonthContext, until: datetime) -> tuple[MonthUsage, dict[str, int]]:
    """Like usage_until, with the filled quarters counted per series."""
    _, afname, injectie, gestuurd, invalid = _measure(ctx, until)
    usage = MonthUsage(
        days=_local_days(ctx.month_start, until),
        days_in_month=_days_in_month(ctx.month_start),
        kwh_in=sum(afname.values(), ZERO),
        kwh_out=sum(injectie.values(), ZERO) if ctx.injectie is not None else None,
        priced_in=_priced(afname, ctx.prices),
        priced_out=_priced(injectie, ctx.prices),
        capacity_kw=ctx.capacity_kw,
        steered_kwh=sum(gestuurd.values(), ZERO) if ctx.gestuurd is not None else None,
    )
    return usage, invalid


def _slot_floor(moment: datetime) -> datetime:
    return moment.replace(minute=moment.minute - moment.minute % 15, second=0, microsecond=0)


def _per_slot(so_far: Decimal, elapsed: Decimal, previous: Decimal | None) -> Decimal:
    """kWh per remaining quarter, blending this month with the previous one."""
    this = so_far / elapsed if elapsed > 0 else None
    if this is None and previous is None:
        return ZERO
    if this is None:
        per_day = previous
    elif previous is None:
        per_day = this
    else:
        weight = min(Decimal(1), elapsed / BLEND_DAYS)
        per_day = weight * this + (1 - weight) * previous
    return per_day / SLOTS_PER_DAY


def _remaining_priced(
    remaining: list[datetime],
    per_slot: Decimal,
    priced: tuple[Decimal, Decimal],
    prices: Mapping[datetime, Decimal],
) -> tuple[Decimal, Decimal]:
    kwh, cost = priced
    average = cost / kwh if kwh > 0 else None
    for q in remaining:
        price = prices.get(q, average)
        if price is not None:
            kwh += per_slot
            cost += per_slot * price
    return kwh, cost


def forecast(ctx: MonthContext, rules: TariffRules, params: FitParams) -> Forecast:
    """'So far' and 'expected' for the month of `ctx`."""
    until = max(_slot_floor(ctx.now), ctx.month_start)
    so_far_usage, invalid = usage_until(ctx, until)
    so_far = price_month(so_far_usage, rules, params)
    elapsed = so_far_usage.days
    closed = set(_closed(ctx, until))
    remaining = [q for q in month_slots(ctx.month_start) if q not in closed]
    prev = ctx.previous
    in_hour = _per_slot(so_far_usage.kwh_in, elapsed, prev.kwh_in_per_day if prev else None)
    kwh_out = so_far_usage.kwh_out
    out_hour = _per_slot(kwh_out or ZERO, elapsed, prev.kwh_out_per_day if prev else None)
    steered = None
    if so_far_usage.steered_kwh is not None:
        steer_hour = _per_slot(
            so_far_usage.steered_kwh, elapsed, prev.steered_per_day if prev else None
        )
        steered = so_far_usage.steered_kwh + steer_hour * len(remaining)
    days_in_month = so_far_usage.days_in_month
    expected_usage = MonthUsage(
        days=Decimal(days_in_month),
        days_in_month=days_in_month,
        kwh_in=so_far_usage.kwh_in + in_hour * len(remaining),
        kwh_out=kwh_out + out_hour * len(remaining) if kwh_out is not None else None,
        priced_in=_remaining_priced(remaining, in_hour, so_far_usage.priced_in, ctx.prices),
        priced_out=_remaining_priced(remaining, out_hour, so_far_usage.priced_out, ctx.prices),
        capacity_kw=ctx.capacity_kw,
        steered_kwh=steered,
    )
    expected = price_month(expected_usage, rules, params)
    day = ctx.now.astimezone(ctx.month_start.tzinfo).day
    daily = cumulative_series(ctx, rules, params, last_day=day - 1) + ((day, so_far.total),)
    missing = set(so_far.missing | expected.missing)
    if invalid:
        missing.add("meting")
    return Forecast(
        so_far=so_far,
        expected=expected,
        elapsed_days=elapsed,
        day=day,
        days_in_month=days_in_month,
        daily=daily,
        missing=frozenset(missing),
    )


def cumulative_series(
    ctx: MonthContext, rules: TariffRules, params: FitParams, last_day: int
) -> tuple[tuple[int, Decimal], ...]:
    """Cumulative bill total at the end of each day 1..last_day (one pass over the slots)."""
    if last_day < 1:
        return ()
    tz = ctx.month_start.tzinfo
    until = max(_slot_floor(ctx.now), ctx.month_start)
    slots, afname, injectie, gestuurd, _ = _measure(ctx, until)
    base = ctx.month_start.replace(tzinfo=None)
    kwh_in = kwh_out = steered = ZERO
    in_kwh = in_cost = out_kwh = out_cost = ZERO
    index, series = 0, []
    for day in range(1, last_day + 1):
        end = min((base + timedelta(days=day)).replace(tzinfo=tz), until)
        while index < len(slots) and slots[index] + SLOT <= end:
            q = slots[index]
            value_in = afname.get(q, ZERO)
            kwh_in += value_in
            price = ctx.prices.get(q)
            if price is not None:
                in_kwh += value_in
                in_cost += value_in * price
            if ctx.injectie is not None:
                value_out = injectie.get(q, ZERO)
                kwh_out += value_out
                if price is not None:
                    out_kwh += value_out
                    out_cost += value_out * price
            if ctx.gestuurd is not None:
                steered += gestuurd.get(q, ZERO)
            index += 1
        usage = MonthUsage(
            days=_local_days(ctx.month_start, end),
            days_in_month=_days_in_month(ctx.month_start),
            kwh_in=kwh_in,
            kwh_out=kwh_out if ctx.injectie is not None else None,
            priced_in=(in_kwh, in_cost),
            priced_out=(out_kwh, out_cost),
            capacity_kw=ctx.capacity_kw,
            steered_kwh=steered if ctx.gestuurd is not None else None,
        )
        series.append((day, price_month(usage, rules, params).total))
    return tuple(series)

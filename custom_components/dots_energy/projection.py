"""Turn hourly series of the running month into 'so far' and 'expected' bills.

Pure Python; hours are UTC datetimes at the start of the hour, months are
calendar months in the local time zone of `month_start`.
"""

from __future__ import annotations

import calendar
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from .tariff import Bill, FitParams, MonthUsage, TariffRules, price_month

MAX_HOURLY_KWH = Decimal(50)
BLEND_DAYS = Decimal(7)
HOUR = timedelta(hours=1)
ZERO = Decimal(0)


@dataclass(frozen=True)
class HourlySeries:
    """kWh per hour (UTC start of hour); absent = not measured."""

    values: Mapping[datetime, Decimal]


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
    afname: HourlySeries
    injectie: HourlySeries | None
    gestuurd: HourlySeries | None
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


def month_hours(month_start: datetime) -> list[datetime]:
    """UTC starts of every hour in the local calendar month (DST-correct)."""
    hour = month_start.astimezone(UTC)
    end = _next_month(month_start).astimezone(UTC)
    hours = []
    while hour < end:
        hours.append(hour)
        hour += HOUR
    return hours


def _local_days(month_start: datetime, until: datetime) -> Decimal:
    """Calendar days between the month start and `until`, in local wall time."""
    local = until.astimezone(month_start.tzinfo).replace(tzinfo=None)
    seconds = (local - month_start.replace(tzinfo=None)).total_seconds()
    return Decimal(int(seconds)) / Decimal(86400)


def _closed(ctx: MonthContext, until: datetime) -> list[datetime]:
    return [h for h in month_hours(ctx.month_start) if h + HOUR <= until]


def _filled(
    series: HourlySeries | None, hours: list[datetime], tz
) -> tuple[dict[datetime, Decimal], int]:
    """Valid values; invalid or missing hours filled with a same-hour average."""
    if series is None:
        return {}, 0
    valid: dict[datetime, Decimal] = {}
    for h in hours:
        value = series.values.get(h)
        if value is not None and ZERO <= value <= MAX_HOURLY_KWH:
            valid[h] = value
    by_hour: dict[int, list[Decimal]] = {}
    for h, value in valid.items():
        by_hour.setdefault(h.astimezone(tz).hour, []).append(value)
    overall = sum(valid.values(), ZERO) / len(valid) if valid else ZERO
    result, invalid = dict(valid), 0
    for h in hours:
        if h in valid:
            continue
        invalid += 1
        same = by_hour.get(h.astimezone(tz).hour)
        result[h] = sum(same, ZERO) / len(same) if same else overall
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
    return hours, afname, injectie, gestuurd, bad_in + bad_out + bad_steer


def usage_until(ctx: MonthContext, until: datetime) -> tuple[MonthUsage, int]:
    """Usage of the closed hours before `until`, and the number of filled hours."""
    _, afname, injectie, gestuurd, invalid = _measure(ctx, until)
    usage = MonthUsage(
        days=_local_days(ctx.month_start, until),
        days_in_month=_days_in_month(ctx.month_start),
        kwh_in=sum(afname.values(), ZERO),
        kwh_out=sum(injectie.values(), ZERO),
        priced_in=_priced(afname, ctx.prices),
        priced_out=_priced(injectie, ctx.prices),
        capacity_kw=ctx.capacity_kw,
        steered_kwh=sum(gestuurd.values(), ZERO) if ctx.gestuurd is not None else None,
    )
    return usage, invalid


def _hour_floor(moment: datetime) -> datetime:
    return moment.replace(minute=0, second=0, microsecond=0)


def _per_hour(so_far: Decimal, elapsed: Decimal, previous: Decimal | None) -> Decimal:
    """kWh per remaining hour, blending this month with the previous one."""
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
    return per_day / 24


def _remaining_priced(
    remaining: list[datetime],
    per_hour: Decimal,
    priced: tuple[Decimal, Decimal],
    prices: Mapping[datetime, Decimal],
) -> tuple[Decimal, Decimal]:
    kwh, cost = priced
    average = cost / kwh if kwh > 0 else None
    for h in remaining:
        price = prices.get(h, average)
        if price is not None:
            kwh += per_hour
            cost += per_hour * price
    return kwh, cost


def forecast(ctx: MonthContext, rules: TariffRules, params: FitParams) -> Forecast:
    """'So far' and 'expected' for the month of `ctx`."""
    until = max(_hour_floor(ctx.now), ctx.month_start)
    so_far_usage, invalid = usage_until(ctx, until)
    so_far = price_month(so_far_usage, rules, params)
    elapsed = so_far_usage.days
    closed = set(_closed(ctx, until))
    remaining = [h for h in month_hours(ctx.month_start) if h not in closed]
    prev = ctx.previous
    in_hour = _per_hour(so_far_usage.kwh_in, elapsed, prev.kwh_in_per_day if prev else None)
    out_hour = _per_hour(so_far_usage.kwh_out, elapsed, prev.kwh_out_per_day if prev else None)
    steered = None
    if so_far_usage.steered_kwh is not None:
        steer_hour = _per_hour(
            so_far_usage.steered_kwh, elapsed, prev.steered_per_day if prev else None
        )
        steered = so_far_usage.steered_kwh + steer_hour * len(remaining)
    days_in_month = so_far_usage.days_in_month
    expected_usage = MonthUsage(
        days=Decimal(days_in_month),
        days_in_month=days_in_month,
        kwh_in=so_far_usage.kwh_in + in_hour * len(remaining),
        kwh_out=so_far_usage.kwh_out + out_hour * len(remaining),
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
    """Cumulative bill total at the end of each day 1..last_day."""
    tz = ctx.month_start.tzinfo
    series = []
    for day in range(1, last_day + 1):
        end = (ctx.month_start.replace(tzinfo=None) + timedelta(days=day)).replace(tzinfo=tz)
        usage, _ = usage_until(ctx, min(end, max(_hour_floor(ctx.now), ctx.month_start)))
        series.append((day, price_month(usage, rules, params).total))
    return tuple(series)

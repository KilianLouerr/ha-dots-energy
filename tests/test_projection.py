"""Projecting the running month from hourly series."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from custom_components.dots_energy.projection import (
    HourlySeries,
    MonthContext,
    MonthMeasuredDaily,
    cumulative_series,
    forecast,
    month_hours,
    usage_until,
    usage_until_by_series,
)
from custom_components.dots_energy.tariff import FitParams, derive_rules

from .forecast_data import breakdowns, detail

D = Decimal
TZ = ZoneInfo("Europe/Brussels")
JAN = datetime(2026, 1, 1, tzinfo=TZ)


def _rules():
    d = detail(period="2025-12")
    return derive_rules([d], breakdowns(d))


def _series(start: datetime, until: datetime, kwh=1, overrides=None) -> HourlySeries:
    values = {}
    for h in month_hours(start):
        if h + timedelta(hours=1) <= until:
            values[h] = D(str(kwh))
    values.update(overrides or {})
    return HourlySeries(values=values)


def _ctx(now, *, kwh=1, overrides=None, prices=None, previous=None, injectie=None):
    return MonthContext(
        month_start=JAN,
        now=now,
        afname=_series(JAN, now, kwh, overrides),
        injectie=injectie,
        gestuurd=None,
        prices=prices or {},
        capacity_kw=None,
        previous=previous,
    )


def _energy(bill):
    return next(line for line in bill.lines if line.description == "Elektriciteit totale uren")


def test_month_hours_counts():
    assert len(month_hours(JAN)) == 744
    assert len(month_hours(datetime(2026, 3, 1, tzinfo=TZ))) == 743
    assert all(h.tzinfo == UTC for h in month_hours(JAN))


def test_october_has_745_hours():
    hours = month_hours(datetime(2026, 10, 1, tzinfo=TZ))
    assert len(hours) == 745
    assert len(set(hours)) == 745


def test_so_far_counts_only_closed_hours():
    now = datetime(2026, 1, 3, 10, 30, tzinfo=TZ)
    ctx = _ctx(now)
    usage, invalid = usage_until(ctx, datetime(2026, 1, 3, 10, tzinfo=TZ))
    assert usage.kwh_in == 58
    assert usage.days == D(58) / 24
    assert invalid == 0
    assert forecast(ctx, _rules(), FitParams()).elapsed_days == D(58) / 24


def test_projection_discards_reset_hour():
    now = datetime(2026, 1, 3, 10, 0, tzinfo=TZ)
    bad = datetime(2026, 1, 2, 5, tzinfo=TZ).astimezone(UTC)
    ctx = _ctx(now, overrides={bad: D(-5000)})
    usage, invalid = usage_until(ctx, now)
    assert invalid == 1
    assert usage.kwh_in == 58
    assert "meting" in forecast(ctx, _rules(), FitParams()).missing


def test_missing_hour_is_filled():
    now = datetime(2026, 1, 3, 10, 0, tzinfo=TZ)
    gap = datetime(2026, 1, 2, 5, tzinfo=TZ).astimezone(UTC)
    ctx = _ctx(now)
    del ctx.afname.values[gap]
    usage, invalid = usage_until(ctx, now)
    assert invalid == 1
    assert usage.kwh_in == 58


def test_day_one_before_first_hour():
    now = datetime(2026, 1, 1, 0, 30, tzinfo=TZ)
    previous = MonthMeasuredDaily(kwh_in_per_day=D(10), kwh_out_per_day=D(0), steered_per_day=None)
    result = forecast(_ctx(now, previous=previous), _rules(), FitParams())
    assert result.so_far.total == 0
    assert _energy(result.expected).amount == D("37.20")
    assert result.day == 1
    assert result.daily == ((1, D(0)),)


def test_expected_blends_previous_month():
    now = datetime(2026, 1, 4, 12, 0, tzinfo=TZ)
    previous = MonthMeasuredDaily(kwh_in_per_day=D(24), kwh_out_per_day=D(0), steered_per_day=None)
    result = forecast(_ctx(now, kwh=2, previous=previous), _rules(), FitParams())
    # 168 kWh so far (48/day), then 27.5 days at 0.5*48 + 0.5*24 = 36 kWh/day -> 1158 kWh
    assert _energy(result.expected).amount == D("138.96")


def test_expected_uses_known_future_prices():
    now = datetime(2026, 1, 31, 22, 0, tzinfo=TZ)
    late = datetime(2026, 1, 31, 22, tzinfo=TZ).astimezone(UTC)
    prices = {h: D("0.10") for h in month_hours(JAN) if h + timedelta(hours=1) <= now}
    prices[late] = D("0.50")
    result = forecast(_ctx(now, prices=prices), _rules(), FitParams(energy_markup=D(0)))
    assert _energy(result.expected).amount == D("74.80")
    assert "marktprijs" not in result.expected.missing


def test_cumulative_series_is_non_decreasing_without_injection():
    now = datetime(2026, 1, 10, 6, 0, tzinfo=TZ)
    ctx = _ctx(now)
    series = cumulative_series(ctx, _rules(), FitParams(), last_day=9)
    assert [day for day, _ in series] == list(range(1, 10))
    totals = [total for _, total in series]
    assert totals == sorted(totals)
    result = forecast(ctx, _rules(), FitParams())
    assert result.daily[:-1] == series
    assert result.daily[-1] == (10, result.so_far.total)


def test_invalid_hours_counted_per_series():
    now = datetime(2026, 1, 3, 10, 0, tzinfo=TZ)
    bad = datetime(2026, 1, 2, 5, tzinfo=TZ).astimezone(UTC)
    out = _series(JAN, now, 1, {bad: D(-3)})
    ctx = _ctx(now, injectie=out)
    usage, invalid = usage_until_by_series(ctx, now)
    assert invalid == {"afname": 0, "injectie": 1, "gestuurd": 0}
    assert usage.kwh_out == 58

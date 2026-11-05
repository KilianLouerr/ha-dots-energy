"""Projecting the running month from hourly series."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from custom_components.dots_energy.projection import (
    MonthContext,
    MonthMeasuredDaily,
    SlotSeries,
    cumulative_series,
    forecast,
    merge_quarters,
    month_slots,
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


def _series(start: datetime, until: datetime, kwh=1, overrides=None) -> SlotSeries:
    """kwh per hour, as four equal quarters per hour."""
    values = {}
    for q in month_slots(start):
        if q + timedelta(minutes=15) <= until:
            values[q] = D(str(kwh)) / 4
    values.update(overrides or {})
    return SlotSeries(values=values)


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


def test_month_slots_counts():
    assert len(month_slots(JAN)) == 744 * 4
    assert len(month_slots(datetime(2026, 3, 1, tzinfo=TZ))) == 743 * 4
    assert all(q.tzinfo == UTC for q in month_slots(JAN))


def test_october_has_745_hours_of_quarters():
    slots = month_slots(datetime(2026, 10, 1, tzinfo=TZ))
    assert len(slots) == 745 * 4
    assert len(set(slots)) == 745 * 4


def test_spread_hourly_splits_in_four_quarters():
    hour = datetime(2026, 1, 1, 10, tzinfo=UTC)
    spread = merge_quarters({hour: D(2)}, {})
    assert spread == {hour + timedelta(minutes=15 * i): D("0.5") for i in range(4)}


def test_so_far_counts_only_closed_quarters():
    now = datetime(2026, 1, 3, 10, 40, tzinfo=TZ)
    ctx = _ctx(now)
    usage, invalid = usage_until(ctx, datetime(2026, 1, 3, 10, tzinfo=TZ))
    assert usage.kwh_in == 58
    assert usage.days == D(58) / 24
    assert invalid == 0
    assert forecast(ctx, _rules(), FitParams()).elapsed_days == D("58.5") / 24


def test_projection_discards_reset_hour():
    now = datetime(2026, 1, 3, 10, 0, tzinfo=TZ)
    bad = datetime(2026, 1, 2, 5, 15, tzinfo=TZ).astimezone(UTC)
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


def test_day_one_before_first_quarter():
    now = datetime(2026, 1, 1, 0, 10, tzinfo=TZ)
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
    prices = {q: D("0.10") for q in month_slots(JAN) if q + timedelta(minutes=15) <= now}
    prices[late] = D("0.50")
    result = forecast(_ctx(now, prices=prices), _rules(), FitParams(energy_markup=D(0)))
    # 742 kWh at 0.10, then 8 remaining quarters of 0.25 kWh: one at 0.50, seven at 0.10
    assert _energy(result.expected).amount == D("74.50")
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


def test_no_injection_source_marks_injection_missing():
    now = datetime(2026, 1, 10, 0, 0, tzinfo=TZ)
    result = forecast(_ctx(now, injectie=None), _rules(), FitParams())
    settlement = next(
        line
        for line in result.expected.lines
        if line.description == "Settlement injectie totale uren"
    )
    assert settlement.amount < 0
    assert "injectie" in result.missing


def test_quarter_prices_weight_quarter_usage():
    """Usage in the cheap quarter of an hour is priced at that quarter, not the hour average."""
    now = datetime(2026, 1, 1, 1, 0, tzinfo=TZ)
    q0 = datetime(2026, 1, 1, 0, 0, tzinfo=TZ).astimezone(UTC)
    quarters = [q0 + timedelta(minutes=15 * i) for i in range(4)]
    afname = SlotSeries(values={quarters[0]: D(4), **{q: D(0) for q in quarters[1:]}})
    prices = {quarters[0]: D("0.05"), **{q: D("0.25") for q in quarters[1:]}}
    ctx = MonthContext(
        month_start=JAN,
        now=now,
        afname=afname,
        injectie=None,
        gestuurd=None,
        prices=prices,
        capacity_kw=None,
        previous=None,
    )
    usage, _ = usage_until(ctx, now)
    assert usage.priced_in == (D(4), D("0.20"))


def test_slot_above_limit_is_invalid():
    now = datetime(2026, 1, 1, 1, 0, tzinfo=TZ)
    q0 = datetime(2026, 1, 1, 0, 0, tzinfo=TZ).astimezone(UTC)
    ctx = _ctx(now, overrides={q0: D(13)})
    _, invalid = usage_until(ctx, now)
    assert invalid == 1


def test_merge_hourly_with_partial_quarters_keeps_hour_total():
    hour = datetime(2026, 1, 1, 10, tzinfo=UTC)
    q = timedelta(minutes=15)
    merged = merge_quarters({hour: D(2)}, {hour: D("1.4")})
    assert merged[hour] == D("1.4")
    assert [merged[hour + q * i] for i in (1, 2, 3)] == [D("0.2")] * 3
    assert sum(merged.values()) == D(2)


def test_merge_quarters_without_hourly_value():
    hour = datetime(2026, 1, 1, 10, tzinfo=UTC)
    merged = merge_quarters({}, {hour + timedelta(minutes=30): D("0.7")})
    assert merged == {hour + timedelta(minutes=30): D("0.7")}


def test_merge_ignores_quarters_that_do_not_add_up_to_the_hour():
    hour = datetime(2026, 1, 1, 10, tzinfo=UTC)
    q = timedelta(minutes=15)
    quarters = {hour + q * i: D("5000") if i == 0 else D("0.1") for i in range(4)}
    merged = merge_quarters({hour: D(2)}, quarters)
    assert [merged[hour + q * i] for i in range(4)] == [D("0.5")] * 4


def test_merge_ignores_negative_quarter():
    hour = datetime(2026, 1, 1, 10, tzinfo=UTC)
    merged = merge_quarters({hour: D(2)}, {hour: D("-0.3")})
    assert merged[hour] == D("0.5")


def test_open_hour_without_quarters_falls_back_to_hour_start():
    """At 12:40 with no 5-minute data yet for 12:00-12:30, count only up to 12:00."""
    now = datetime(2026, 1, 3, 12, 40, tzinfo=TZ)
    hour = datetime(2026, 1, 3, 12, 0, tzinfo=TZ)
    ctx = _ctx(now)
    for q in (hour, hour + timedelta(minutes=15)):
        ctx.afname.values.pop(q.astimezone(UTC), None)
    result = forecast(ctx, _rules(), FitParams())
    assert "meting" not in result.missing
    assert result.elapsed_days == D(60) / 24

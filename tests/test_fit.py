"""Fitting markups and rates on past invoices."""

from __future__ import annotations

from decimal import Decimal

from custom_components.dots_energy.fit import MonthMeasured, eligible, fit_params
from custom_components.dots_energy.tariff import MonthUsage

from .forecast_data import breakdowns, detail

D = Decimal


def measured(
    month: str,
    *,
    kwh_in=200,
    kwh_out=50,
    priced_in=None,
    priced_out=None,
    capacity=None,
    steered=None,
    invalid=0,
) -> MonthMeasured:
    k_in, k_out = D(str(kwh_in)), D(str(kwh_out))
    return MonthMeasured(
        month=month,
        usage=MonthUsage(
            days=D(31),
            days_in_month=31,
            kwh_in=k_in,
            kwh_out=k_out,
            priced_in=priced_in or (D(0), D(0)),
            priced_out=priced_out or (D(0), D(0)),
            capacity_kw=None if capacity is None else D(str(capacity)),
            steered_kwh=None if steered is None else D(str(steered)),
        ),
        invalid_hours=invalid,
    )


def test_month_with_invalid_hour_not_eligible():
    d = detail(period="2026-01", kwh_in=200)
    assert eligible(measured("2026-01"), d)
    assert not eligible(measured("2026-01", invalid=1), d)


def test_month_more_than_3_percent_off_not_eligible():
    d = detail(period="2026-01", kwh_in=200)
    assert not eligible(measured("2026-01", kwh_in=207), d)
    assert eligible(measured("2026-01", kwh_in=205), d)


def test_energy_markup_is_mean_difference():
    jan = detail(invoice_id=1, period="2026-01", energy_price=0.15)
    feb = detail(invoice_id=2, period="2026-02", energy_price=0.14)
    m = {
        "2026-01": measured("2026-01", priced_in=(D(200), D(24))),
        "2026-02": measured("2026-02", priced_in=(D(200), D(24))),
    }
    params = fit_params([jan, feb], breakdowns(jan, feb), m)
    assert params.energy_markup == D("0.025")


def test_injection_markup_is_mean_difference():
    jan = detail(period="2026-01", injection_price=0.10)
    m = {"2026-01": measured("2026-01", priced_out=(D(50), D(4)))}
    params = fit_params([jan], breakdowns(jan), m)
    assert params.injection_markup == D("0.02")


def test_markup_needs_80_percent_priced():
    jan = detail(period="2026-01", energy_price=0.15)
    m = {"2026-01": measured("2026-01", priced_in=(D(150), D(18)))}
    assert fit_params([jan], breakdowns(jan), m).energy_markup is None


def test_ineligible_month_not_used():
    jan = detail(period="2026-01", energy_price=0.15)
    m = {"2026-01": measured("2026-01", priced_in=(D(200), D(24)), invalid=2)}
    assert fit_params([jan], breakdowns(jan), m).energy_markup is None


def _transport_months(n):
    rows = [("2026-01", 3, 200), ("2026-02", 5, 300), ("2026-03", 4, 100)][:n]
    details = [
        detail(invoice_id=i + 1, period=p, kwh_in=k, transport=4 * c + 0.05 * k)
        for i, (p, c, k) in enumerate(rows)
    ]
    m = {p: measured(p, kwh_in=k, capacity=c) for p, c, k in rows}
    return details, m


def test_transport_fit_needs_three_months():
    details, m = _transport_months(2)
    params = fit_params(details, breakdowns(*details), m)
    assert params.capacity_rate is None
    assert params.transport_kwh_rate is None


def test_transport_fit_recovers_a_and_b():
    details, m = _transport_months(3)
    params = fit_params(details, breakdowns(*details), m)
    assert abs(params.capacity_rate - D(4)) < D("0.001")
    assert abs(params.transport_kwh_rate - D("0.05")) < D("0.001")


def test_flex_per_kwh():
    jan = detail(invoice_id=1, period="2026-01", flex=6.0)
    feb = detail(invoice_id=2, period="2026-02", flex=12.0)
    m = {
        "2026-01": measured("2026-01", steered=150),
        "2026-02": measured("2026-02", steered=225),
    }
    assert fit_params([jan, feb], breakdowns(jan, feb), m).flex_per_kwh == D("0.048")


def test_credit_notes_ignored():
    credit = detail(period="2026-01", energy_price=0.5, invoice_type="out_refund")
    m = {"2026-01": measured("2026-01", priced_in=(D(200), D(24)))}
    assert fit_params([credit], breakdowns(credit), m).energy_markup is None


def test_nothing_measured_gives_empty_params():
    jan = detail(period="2026-01")
    params = fit_params([jan], breakdowns(jan), {})
    assert params.energy_markup is None
    assert params.flex_per_kwh is None

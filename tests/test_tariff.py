"""Tariff model: rules from invoices, pricing a month."""

from __future__ import annotations

from decimal import Decimal

from custom_components.dots_energy.const import Category
from custom_components.dots_energy.tariff import (
    FitParams,
    MonthUsage,
    derive_rules,
    price_month,
)

from .forecast_data import breakdowns, detail

D = Decimal


def usage(**overrides) -> MonthUsage:
    base = {
        "days": D(31),
        "days_in_month": 31,
        "kwh_in": D(200),
        "kwh_out": D(50),
        "priced_in": (D(0), D(0)),
        "priced_out": (D(0), D(0)),
        "capacity_kw": None,
        "steered_kwh": None,
    }
    return MonthUsage(**(base | overrides))


def _rules(**kwargs):
    d = detail(**kwargs)
    return derive_rules([d], breakdowns(d)), d


def _line(bill, description):
    return next(line for line in bill.lines if line.description == description)


def test_derive_rules_classifies_lines():
    rules, _ = _rules(period="2026-01", kwh_in=200)
    by = {r.description: r for r in rules.rules}
    assert by["Bijzondere accijns elektriciteit"].basis == "kwh_in"
    assert by["Groenestroomcertificaten"].basis == "mwh_in"
    assert by["Evenwichtsbijdrage injectie"].basis == "kwh_out"
    assert by["Evenwichtsbijdrage injectie"].taxable is False
    assert by["Elektriciteit totale uren"].basis == "energy"
    assert by["Transport en distributie elektriciteit"].basis == "transport"
    assert by["Settlement injectie totale uren"].basis == "injection"
    assert by["Flexibiliteitsvergoeding"].basis == "flex"
    assert by["Vaste vergoeding elektriciteit"].basis == "day"
    assert by["Vaste vergoeding elektriciteit"].rate == D("10.19") / 31
    assert rules.vat_rate == D("0.06")
    assert rules.energy_unit_price == D("0.12")
    assert rules.injection_unit_price == D("0.1")


def test_price_month_reproduces_invoice():
    rules, d = _rules(period="2026-01")
    bill = price_month(usage(), rules, FitParams())
    for line in d.lines:
        if line.kind == "charge":
            assert abs(_line(bill, line.description).amount - line.amount) <= D("0.01")
    assert bill.total == d.summary.total
    assert bill.by_category()[Category.BTW] == d.summary.tax


def test_energy_without_prices_uses_invoice_unit_price():
    rules, _ = _rules()
    bill = price_month(usage(priced_in=(D(0), D(0))), rules, FitParams(energy_markup=D("0.02")))
    assert _line(bill, "Elektriciteit totale uren").amount == D("24.00")
    assert "marktprijs" in bill.missing


def test_energy_without_markup_is_marked_missing():
    rules, _ = _rules()
    bill = price_month(usage(priced_in=(D(200), D(20))), rules, FitParams())
    assert _line(bill, "Elektriciteit totale uren").amount == D("24.00")
    assert "marktprijs" in bill.missing


def test_energy_with_markup():
    rules, _ = _rules()
    bill = price_month(
        usage(kwh_in=D(100), priced_in=(D(100), D("9.0"))),
        rules,
        FitParams(energy_markup=D("0.02"), injection_markup=D("-0.01")),
    )
    assert _line(bill, "Elektriciteit totale uren").amount == D("11.00")


def test_injection_with_markup_is_negative():
    rules, _ = _rules()
    bill = price_month(
        usage(kwh_out=D(50), priced_out=(D(50), D("4.0"))),
        rules,
        FitParams(energy_markup=D(0), injection_markup=D("-0.01")),
    )
    assert _line(bill, "Settlement injectie totale uren").amount == D("-3.50")


def test_transport_capacity_fit_pro_rata():
    rules, _ = _rules()
    bill = price_month(
        usage(days=D(15), days_in_month=30, kwh_in=D(100), capacity_kw=D(4)),
        rules,
        FitParams(capacity_rate=D(4), transport_kwh_rate=D("0.05")),
    )
    assert _line(bill, "Transport en distributie elektriciteit").amount == D("13.00")


def test_transport_without_capacity_falls_back_per_day():
    rules, _ = _rules(period="2026-01", transport=31)
    bill = price_month(
        usage(days=D(10)), rules, FitParams(capacity_rate=D(4), transport_kwh_rate=D("0.05"))
    )
    assert _line(bill, "Transport en distributie elektriciteit").amount == D("10.00")
    assert "capaciteit" in bill.missing


def test_flex_fallback_per_day():
    rules, _ = _rules(period="2026-01", flex=6.2)
    bill = price_month(usage(days=D(10)), rules, FitParams())
    assert _line(bill, "Flexibiliteitsvergoeding").amount == D("-2.00")
    assert "gestuurde_kwh" not in bill.missing


def test_flex_per_kwh():
    rules, _ = _rules()
    bill = price_month(usage(steered_kwh=D(100)), rules, FitParams(flex_per_kwh=D("0.05")))
    assert _line(bill, "Flexibiliteitsvergoeding").amount == D("-5.00")


def test_flex_per_kwh_without_steered_data_is_missing():
    rules, _ = _rules()
    bill = price_month(usage(steered_kwh=None), rules, FitParams(flex_per_kwh=D("0.05")))
    assert "gestuurde_kwh" in bill.missing


def test_vat_excludes_injection_balancing_and_discounts():
    rules, _ = _rules()
    bill = price_month(usage(), rules, FitParams())
    taxable = sum(
        (
            line.amount
            for line in bill.lines
            if line.category != Category.BTW
            and line.amount > 0
            and line.description != "Evenwichtsbijdrage injectie"
        ),
        D(0),
    )
    vat = _line(bill, "6%")
    assert vat.quantity == taxable
    assert vat.amount == (taxable * D("0.06")).quantize(D("0.01"))


def test_by_category_sums_to_total():
    rules, _ = _rules()
    bill = price_month(usage(days=D("12.5"), kwh_in=D("77.7")), rules, FitParams())
    cats = bill.by_category()
    assert set(cats) == set(Category)
    assert sum(cats.values(), D(0)) == bill.total


def test_derive_rules_none_without_details():
    assert derive_rules([], {}) is None


def test_derive_rules_takes_line_from_older_invoice_when_missing_in_newest():
    older = detail(invoice_id=1, period="2026-01")
    newest = detail(invoice_id=2, period="2026-02", energy_price=0.2)
    newest = type(newest)(
        summary=newest.summary,
        lines=tuple(
            line for line in newest.lines if line.description != "Groenestroomcertificaten"
        ),
    )
    rules = derive_rules([older, newest], breakdowns(older, newest))
    by = {r.description: r for r in rules.rules}
    assert "Groenestroomcertificaten" in by
    assert rules.energy_unit_price == D("0.2")


def test_derive_rules_ignores_credit_notes():
    regular = detail(invoice_id=1, period="2026-01")
    credit = detail(invoice_id=2, period="2026-02", energy_price=0.5, invoice_type="out_refund")
    rules = derive_rules([regular, credit], breakdowns(regular, credit))
    assert rules.energy_unit_price == D("0.12")

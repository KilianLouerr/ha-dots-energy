"""Tariff model: derive pricing rules from past invoices and price a month with them.

Pure Python, no Home Assistant imports, so the same model can price other
scenarios later (for example a month without the home battery).
"""

from __future__ import annotations

import calendar
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from .categories import Breakdown, breakdown, categorize_line
from .const import Category
from .kpis import is_credit_note
from .models import InvoiceDetail, InvoiceLine

Basis = Literal["energy", "injection", "kwh_in", "mwh_in", "kwh_out", "day", "transport", "flex"]

CENT = Decimal("0.01")
ZERO = Decimal(0)
RULE_LOOKBACK = 3

_KWH_IN = ("evenwichtsbijdrage afname", "accijnzen", "bijzondere accijns")
_MWH_IN = ("groenestroomcertificaten", "warmtekrachtcertificaten")


@dataclass(frozen=True)
class Rule:
    """How one invoice line scales."""

    description: str
    category: Category
    basis: Basis
    rate: Decimal
    taxable: bool


@dataclass(frozen=True)
class TariffRules:
    """Rules and fallback prices taken from the most recent invoices."""

    rules: tuple[Rule, ...]
    vat_rate: Decimal
    energy_unit_price: Decimal | None
    injection_unit_price: Decimal | None
    transport_per_day: Decimal | None
    flex_per_day: Decimal


@dataclass(frozen=True)
class FitParams:
    """Parameters learned from past months; None means not learned (yet)."""

    energy_markup: Decimal | None = None
    injection_markup: Decimal | None = None
    capacity_rate: Decimal | None = None
    transport_kwh_rate: Decimal | None = None
    flex_per_kwh: Decimal | None = None


@dataclass(frozen=True)
class MonthUsage:
    """What happened (or is assumed to happen) in a month, as model input."""

    days: Decimal
    days_in_month: int
    kwh_in: Decimal
    kwh_out: Decimal
    priced_in: tuple[Decimal, Decimal]
    priced_out: tuple[Decimal, Decimal]
    capacity_kw: Decimal | None
    steered_kwh: Decimal | None


@dataclass(frozen=True)
class BillLine:
    """One priced line."""

    description: str
    category: Category
    quantity: Decimal | None
    unit_price: Decimal | None
    amount: Decimal


@dataclass(frozen=True)
class Bill:
    """A priced month."""

    lines: tuple[BillLine, ...]
    missing: frozenset[str]

    @property
    def total(self) -> Decimal:
        """Sum of all lines."""
        return sum((line.amount for line in self.lines), ZERO)

    def by_category(self) -> dict[Category, Decimal]:
        """Amounts per category, every category present."""
        amounts = {category: ZERO for category in Category}
        for line in self.lines:
            amounts[line.category] += line.amount
        return amounts


def _round(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _days_in_period(detail: InvoiceDetail) -> int:
    start = detail.summary.period_start
    return calendar.monthrange(start.year, start.month)[1]


def _newest_first(details: Sequence[InvoiceDetail]) -> list[InvoiceDetail]:
    regular = [d for d in details if not is_credit_note(d.summary)]
    return sorted(
        regular,
        key=lambda d: (d.summary.period_start, d.summary.invoice_date, d.summary.id),
        reverse=True,
    )


def _basis(line: InvoiceLine) -> Basis | None:
    """Basis of a non-tax, non-rounding line."""
    lower = line.description.lower()
    if line.kind == "discount":
        return "injection" if lower.startswith("settlement injectie") else "flex"
    if lower.startswith("elektriciteit totale uren"):
        return "energy"
    if lower.startswith(_MWH_IN):
        return "mwh_in"
    if lower.startswith(_KWH_IN):
        return "kwh_in"
    if lower.startswith("evenwichtsbijdrage injectie"):
        return "kwh_out"
    if lower.startswith("transport en distributie"):
        return "transport"
    return "day"


def _injection_kwh(detail: InvoiceDetail) -> Decimal | None:
    for line in detail.lines:
        if line.kind == "charge" and line.description.lower().startswith(
            "evenwichtsbijdrage injectie"
        ):
            return line.quantity
    return None


def derive_rules(
    details: Sequence[InvoiceDetail], breakdowns: Mapping[int, Breakdown]
) -> TariffRules | None:
    """Rules from the newest regular invoice, completed by the ones before it."""
    recent = _newest_first(details)[:RULE_LOOKBACK]
    if not recent:
        return None
    rules: dict[str, Rule] = {}
    vat_rate: Decimal | None = None
    energy_price: Decimal | None = None
    injection_price: Decimal | None = None
    transport_per_day: Decimal | None = None
    has_flex = False
    for detail in recent:
        days = Decimal(_days_in_period(detail))
        for line in detail.lines:
            category = categorize_line(line)
            if category is None:
                continue
            if line.kind == "tax":
                if vat_rate is None and line.unit_price is not None:
                    vat_rate = line.unit_price / 100
                continue
            basis = _basis(line)
            if basis == "flex":
                if has_flex:
                    continue
                has_flex = True
            key = line.description.lower()
            if key in rules:
                continue
            rate = ZERO
            if basis in ("kwh_in", "mwh_in", "kwh_out"):
                rate = line.unit_price or ZERO
            elif basis == "day":
                rate = line.amount / days
            elif basis == "energy":
                energy_price = line.unit_price
            elif basis == "transport":
                transport_per_day = line.amount / days
            elif basis == "injection":
                kwh = _injection_kwh(detail)
                injection_price = abs(line.amount) / kwh if kwh else line.unit_price
            rules[key] = Rule(
                description=line.description,
                category=category,
                basis=basis,
                rate=rate,
                taxable=line.kind == "charge" and basis != "kwh_out",
            )
    flex_days = []
    for detail in recent:
        b = breakdowns.get(detail.summary.id) or breakdown(detail)
        flex_days.append(-b.amounts[Category.VERGOEDINGEN] / _days_in_period(detail))
    return TariffRules(
        rules=tuple(rules.values()),
        vat_rate=vat_rate if vat_rate is not None else Decimal("0.06"),
        energy_unit_price=energy_price,
        injection_unit_price=injection_price,
        transport_per_day=transport_per_day,
        flex_per_day=sum(flex_days, ZERO) / len(flex_days),
    )


def _market(
    kwh: Decimal,
    priced: tuple[Decimal, Decimal],
    markup: Decimal | None,
    fallback: Decimal | None,
    missing: set[str],
) -> Decimal:
    """kWh priced at market price plus markup; the rest at the invoice unit price."""
    fallback = fallback or ZERO
    if markup is None:
        if kwh > 0:
            missing.add("marktprijs")
        return kwh * fallback
    priced_kwh, priced_cost = priced
    priced_kwh = min(priced_kwh, kwh)
    unpriced = kwh - priced_kwh
    if unpriced > 0:
        missing.add("marktprijs")
    return priced_cost + markup * priced_kwh + unpriced * fallback


def _unit(amount: Decimal, quantity: Decimal) -> Decimal | None:
    return (amount / quantity).quantize(Decimal("0.0001")) if quantity else None


def price_month(usage: MonthUsage, rules: TariffRules, params: FitParams) -> Bill:
    """Price a month line by line, VAT included."""
    missing: set[str] = set()
    lines: list[BillLine] = []
    taxable = ZERO
    for rule in rules.rules:
        quantity: Decimal | None
        unit: Decimal | None
        if rule.basis == "energy":
            raw = _market(
                usage.kwh_in,
                usage.priced_in,
                params.energy_markup,
                rules.energy_unit_price,
                missing,
            )
            quantity, unit = usage.kwh_in, _unit(raw, usage.kwh_in)
        elif rule.basis == "injection":
            raw = -_market(
                usage.kwh_out,
                usage.priced_out,
                params.injection_markup,
                rules.injection_unit_price,
                missing,
            )
            quantity, unit = usage.kwh_out, _unit(-raw, usage.kwh_out)
        elif rule.basis == "kwh_in":
            quantity, unit = usage.kwh_in, rule.rate
            raw = quantity * unit
        elif rule.basis == "mwh_in":
            quantity, unit = usage.kwh_in / 1000, rule.rate
            raw = quantity * unit
        elif rule.basis == "kwh_out":
            quantity, unit = usage.kwh_out, rule.rate
            raw = quantity * unit
        elif rule.basis == "day":
            quantity, unit = usage.days, rule.rate
            raw = quantity * unit
        elif rule.basis == "transport":
            quantity, unit = None, None
            fitted = params.capacity_rate is not None and params.transport_kwh_rate is not None
            if fitted and usage.capacity_kw is not None:
                raw = (
                    params.capacity_rate * usage.capacity_kw * usage.days / usage.days_in_month
                    + params.transport_kwh_rate * usage.kwh_in
                )
            else:
                if fitted:
                    missing.add("capaciteit")
                raw = (rules.transport_per_day or ZERO) * usage.days
        else:  # flex
            if params.flex_per_kwh is not None and usage.steered_kwh is not None:
                quantity, unit = usage.steered_kwh, params.flex_per_kwh
                raw = -(quantity * unit)
            else:
                if params.flex_per_kwh is not None:
                    missing.add("gestuurde_kwh")
                quantity, unit = None, None
                raw = -(rules.flex_per_day * usage.days)
        amount = _round(raw)
        if rule.taxable:
            taxable += amount
        lines.append(BillLine(rule.description, rule.category, quantity, unit, amount))
    lines.append(
        BillLine(
            description=f"{(rules.vat_rate * 100).normalize():f}%",
            category=Category.BTW,
            quantity=taxable,
            unit_price=rules.vat_rate * 100,
            amount=_round(taxable * rules.vat_rate),
        )
    )
    return Bill(lines=tuple(lines), missing=frozenset(missing))

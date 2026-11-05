"""Learn markups and rates from past invoices and what Home Assistant measured."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from .categories import Breakdown, breakdown
from .const import Category
from .kpis import afname_kwh, is_credit_note
from .models import InvoiceDetail
from .tariff import FitParams, MonthUsage, injection_kwh

MAX_KWH_DEVIATION = Decimal("0.03")
MIN_PRICED_SHARE = Decimal("0.8")
MIN_TRANSPORT_MONTHS = 3
PRECISION = Decimal("0.000001")
ZERO = Decimal(0)


@dataclass(frozen=True)
class MonthMeasured:
    """A whole past month as measured in Home Assistant."""

    month: str
    usage: MonthUsage
    invalid_hours: int  # import (afname) hours missing or discarded
    invalid_out: int = 0  # injection hours missing or discarded
    invalid_steered: int = 0  # steered-kWh hours missing or discarded


def eligible(measured: MonthMeasured, detail: InvoiceDetail) -> bool:
    """Whether a measured month is complete and matches its invoice closely enough."""
    if measured.invalid_hours:
        return False
    billed = afname_kwh(detail)
    if not billed:
        return False
    return abs(measured.usage.kwh_in - billed) <= billed * MAX_KWH_DEVIATION


def _line(detail: InvoiceDetail, prefix: str, kind: str = "charge"):
    return next(
        (
            line
            for line in detail.lines
            if line.kind == kind and line.description.lower().startswith(prefix)
        ),
        None,
    )


def _mean(values: list[Decimal]) -> Decimal | None:
    return (sum(values, ZERO) / len(values)).quantize(PRECISION) if values else None


def _markup(price: Decimal | None, kwh: Decimal, priced: tuple[Decimal, Decimal]) -> Decimal | None:
    priced_kwh, priced_cost = priced
    if price is None or kwh <= 0 or priced_kwh <= 0 or priced_kwh < kwh * MIN_PRICED_SHARE:
        return None
    return price - priced_cost / priced_kwh


def _least_squares(rows: list[tuple[Decimal, Decimal, Decimal]]) -> tuple[Decimal, Decimal] | None:
    """Fit y = a*x1 + b*x2 without intercept; None if singular or negative."""
    s11 = sum((x1 * x1 for x1, _, _ in rows), ZERO)
    s12 = sum((x1 * x2 for x1, x2, _ in rows), ZERO)
    s22 = sum((x2 * x2 for _, x2, _ in rows), ZERO)
    s1y = sum((x1 * y for x1, _, y in rows), ZERO)
    s2y = sum((x2 * y for _, x2, y in rows), ZERO)
    det = s11 * s22 - s12 * s12
    if det == 0:
        return None
    a = (s1y * s22 - s2y * s12) / det
    b = (s11 * s2y - s12 * s1y) / det
    if a < 0 or b < 0:
        return None
    return a.quantize(PRECISION), b.quantize(PRECISION)


def fit_params(
    details: Sequence[InvoiceDetail],
    breakdowns: Mapping[int, Breakdown],
    measured: Mapping[str, MonthMeasured],
) -> FitParams:
    """Learn parameters from the months that have both an invoice and a good measurement."""
    energy: list[Decimal] = []
    injection: list[Decimal] = []
    transport: list[tuple[Decimal, Decimal, Decimal]] = []
    flex_amount, flex_kwh = ZERO, ZERO
    for detail in details:
        if is_credit_note(detail.summary):
            continue
        m = measured.get(detail.summary.period_start.strftime("%Y-%m"))
        if m is None or not eligible(m, detail):
            continue
        usage = m.usage
        energy_line = _line(detail, "elektriciteit totale uren")
        markup = _markup(
            energy_line.unit_price if energy_line else None, usage.kwh_in, usage.priced_in
        )
        if markup is not None:
            energy.append(markup)
        settlement = _line(detail, "settlement injectie", kind="discount")
        injected = injection_kwh(detail)
        if settlement is not None and injected and not m.invalid_out:
            markup = _markup(abs(settlement.amount) / injected, usage.kwh_out, usage.priced_out)
            if markup is not None:
                injection.append(markup)
        transport_line = _line(detail, "transport en distributie")
        billed = afname_kwh(detail)
        if transport_line is not None and usage.capacity_kw is not None and billed:
            transport.append((usage.capacity_kw, billed, transport_line.amount))
        if usage.steered_kwh is not None and usage.steered_kwh > 0 and not m.invalid_steered:
            b = breakdowns.get(detail.summary.id) or breakdown(detail)
            flex_amount += -b.amounts[Category.VERGOEDINGEN]
            flex_kwh += usage.steered_kwh
    fitted = _least_squares(transport) if len(transport) >= MIN_TRANSPORT_MONTHS else None
    return FitParams(
        energy_markup=_mean(energy),
        injection_markup=_mean(injection),
        capacity_rate=fitted[0] if fitted else None,
        transport_kwh_rate=fitted[1] if fitted else None,
        flex_per_kwh=(flex_amount / flex_kwh).quantize(PRECISION) if flex_kwh else None,
    )

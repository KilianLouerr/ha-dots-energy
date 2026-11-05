"""Headline figures and the monthly overview."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from .categories import Breakdown, breakdown, breakdown_from_summary
from .const import Category
from .models import InvoiceDetail, InvoiceSummary

_AFNAME_LINE = "elektriciteit totale uren"


@dataclass(frozen=True)
class Kpis:
    """Headline figures shown at the top of the dashboard."""

    last_invoice: InvoiceSummary | None
    total_this_year: Decimal
    average_per_month: Decimal | None
    all_in_price: Decimal | None
    outstanding: Decimal


def afname_kwh(detail: InvoiceDetail) -> Decimal | None:
    """kWh taken from the grid, from the 'Elektriciteit totale uren' charge."""
    for line in detail.lines:
        if line.kind == "charge" and line.description.lower() == _AFNAME_LINE:
            return line.quantity
    return None


def is_credit_note(summary: InvoiceSummary) -> bool:
    """Whether a document is a credit note rather than a regular invoice."""
    return "refund" in summary.invoice_type.lower()


def last_invoice(summaries: Iterable[InvoiceSummary]) -> InvoiceSummary | None:
    """The regular invoice with the most recent period."""
    regular = [s for s in summaries if not is_credit_note(s)]
    if not regular:
        return None
    return max(regular, key=lambda s: (s.period_start, s.invoice_date, s.id))


def _month(d: date) -> date:
    return d.replace(day=1)


def _breakdown_for(summary: InvoiceSummary, details: dict[int, InvoiceDetail]) -> Breakdown:
    detail = details.get(summary.id)
    return breakdown(detail) if detail is not None else breakdown_from_summary(summary)


def _month_totals(summaries: Iterable[InvoiceSummary]) -> dict[date, Decimal]:
    totals: dict[date, Decimal] = {}
    for summary in summaries:
        month = _month(summary.period_start)
        totals[month] = totals.get(month, Decimal(0)) + summary.total
    return totals


def compute_kpis(
    summaries: list[InvoiceSummary], details: dict[int, InvoiceDetail], today: date
) -> Kpis:
    """Compute the headline figures as defined in the design."""
    last = last_invoice(summaries)
    total_this_year = sum(
        (s.total for s in summaries if s.period_start.year == today.year), Decimal(0)
    )
    first_month = date(today.year - (1 if today.month < 12 else 0), (today.month % 12) + 1, 1)
    recent = [t for m, t in _month_totals(summaries).items() if first_month <= m <= _month(today)]
    average = (
        (sum(recent, Decimal(0)) / len(recent)).quantize(Decimal("0.01"), ROUND_HALF_UP)
        if recent
        else None
    )
    all_in = None
    if last is not None and (detail := details.get(last.id)) is not None:
        kwh = afname_kwh(detail)
        if kwh:
            all_in = (last.total / kwh).quantize(Decimal("0.001"), ROUND_HALF_UP)
    outstanding = sum((s.total for s in summaries if s.status != "paid"), Decimal(0))
    return Kpis(
        last_invoice=last,
        total_this_year=total_this_year,
        average_per_month=average,
        all_in_price=all_in,
        outstanding=outstanding,
    )


def monthly_overview(
    summaries: list[InvoiceSummary], details: dict[int, InvoiceDetail]
) -> list[dict[str, Any]]:
    """Amounts per month of periodStart and category, summed over all documents."""
    months: dict[date, dict[Category, Decimal]] = {}
    for summary in summaries:
        amounts = months.setdefault(_month(summary.period_start), {c: Decimal(0) for c in Category})
        for category, amount in _breakdown_for(summary, details).amounts.items():
            amounts[category] += amount
    result: list[dict[str, Any]] = []
    for month in sorted(months):
        amounts = months[month]
        row: dict[str, Any] = {
            "maand": month.strftime("%Y-%m"),
            "totaal": float(sum(amounts.values(), Decimal(0)).quantize(Decimal("0.01"))),
        }
        row.update({c.value: float(a.quantize(Decimal("0.01"))) for c, a in amounts.items()})
        result.append(row)
    return result


def newest_first(summaries: Iterable[InvoiceSummary]) -> list[InvoiceSummary]:
    """Invoices ordered from the most recent period to the oldest."""
    return sorted(summaries, key=lambda s: (s.period_start, s.invoice_date, s.id), reverse=True)


def invoice_label(summary: InvoiceSummary) -> str:
    """Label used in the invoice select, e.g. '2026-02 · TST00000102'."""
    return f"{summary.period_start.strftime('%Y-%m')} · {summary.number}"

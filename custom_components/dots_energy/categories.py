"""Map invoice lines to cost categories.

The invoice total (`totalAmount`) is the truth: whatever the recognised lines do
not explain lands in `overig`, so the categories of an invoice always add up to
its total exactly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from .const import Category
from .models import InvoiceDetail, InvoiceLine, InvoiceSummary

_LOGGER = logging.getLogger(__name__)

DEVIATION_WARNING = Decimal("0.05")
_warned_unknown: set[str] = set()


@dataclass(frozen=True)
class Breakdown:
    """Amounts per category for one invoice."""

    amounts: dict[Category, Decimal]
    deviation: Decimal


def _empty() -> dict[Category, Decimal]:
    return {category: Decimal(0) for category in Category}


def categorize_line(line: InvoiceLine) -> Category | None:
    """Return the category of a line, or None for a rounding line to ignore."""
    description = line.description.lower()
    if line.kind == "tax":
        return Category.BTW
    if description.startswith("afrondingsverschil"):
        return None
    if line.kind == "discount":
        if description.startswith("settlement injectie"):
            return Category.INJECTIE
        return Category.VERGOEDINGEN
    if "vaste vergoeding" in description:
        return Category.VASTE_KOSTEN
    api_category = (line.api_category or "").lower()
    if api_category.startswith("nettarieven"):
        return Category.NETKOSTEN
    if api_category.startswith("toeslagen"):
        return Category.HEFFINGEN
    if api_category.startswith("energielevering"):
        return Category.ENERGIE
    key = f"{line.api_category} / {line.description}"
    if key not in _warned_unknown:
        _warned_unknown.add(key)
        _LOGGER.warning("Onbekende Dots-factuurregel, geteld als 'overig': %s", key)
    return Category.OVERIG


def breakdown(detail: InvoiceDetail) -> Breakdown:
    """Split an invoice into categories that add up to its total."""
    amounts = _empty()
    for line in detail.lines:
        category = categorize_line(line)
        if category is not None:
            amounts[category] += line.amount
    deviation = detail.summary.total - sum(amounts.values(), Decimal(0))
    amounts[Category.OVERIG] += deviation
    if abs(deviation) > DEVIATION_WARNING:
        _LOGGER.warning(
            "Dots-factuur van periode %s: afwijking van %s EUR tussen regels en totaal",
            detail.summary.period_start.isoformat(),
            deviation,
        )
    return Breakdown(amounts=amounts, deviation=deviation)


def breakdown_from_summary(summary: InvoiceSummary) -> Breakdown:
    """Fallback when an invoice's lines are unavailable: everything in overig."""
    amounts = _empty()
    amounts[Category.OVERIG] = summary.total
    return Breakdown(amounts=amounts, deviation=Decimal(0))

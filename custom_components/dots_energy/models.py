"""Data model for Dots Energy invoices.

Payment details (IBAN, payment reference, bank) are deliberately never parsed,
so they cannot end up in storage, attributes, logs or diagnostics.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

_LOGGER = logging.getLogger(__name__)

PARSE_ERRORS = (KeyError, TypeError, ValueError, AttributeError, InvalidOperation)

LineKind = Literal["charge", "discount", "tax"]


@dataclass(frozen=True)
class InvoiceSummary:
    """An invoice as returned by the invoice list."""

    id: int
    number: str
    invoice_date: date
    due_date: date | None
    period_start: date
    period_end: date
    energy_type: str
    invoice_type: str
    total: Decimal
    tax: Decimal
    net: Decimal
    status: str
    updated_at: datetime


@dataclass(frozen=True)
class InvoiceLine:
    """One line of an invoice: a charge, a discount or a tax."""

    kind: LineKind
    api_category: str | None
    description: str
    quantity: Decimal | None
    unit_price: Decimal | None
    amount: Decimal


@dataclass(frozen=True)
class InvoiceDetail:
    """An invoice with all of its lines."""

    summary: InvoiceSummary
    lines: tuple[InvoiceLine, ...]


def _dec(value: Any) -> Decimal:
    return Decimal(str(value))


def _opt_dec(value: Any) -> Decimal | None:
    return None if value is None else _dec(value)


def _opt_date(value: str | None) -> date | None:
    return None if not value else date.fromisoformat(value)


def _parse_summary(data: dict[str, Any]) -> InvoiceSummary:
    return InvoiceSummary(
        id=int(data["id"]),
        number=str(data["invoiceNumber"]),
        invoice_date=date.fromisoformat(data["invoiceDate"]),
        due_date=_opt_date(data.get("dueDate")),
        period_start=date.fromisoformat(data["periodStart"]),
        period_end=date.fromisoformat(data["periodEnd"]),
        energy_type=str(data.get("energyType") or ""),
        invoice_type=str(data.get("invoiceType") or ""),
        total=_dec(data["totalAmount"]),
        tax=_dec(data.get("taxAmount") or 0),
        net=_dec(data.get("netAmount") or 0),
        status=str(data.get("status") or ""),
        updated_at=datetime.fromisoformat(data["updatedAt"]),
    )


def parse_invoice_list(data: list[dict[str, Any]]) -> list[InvoiceSummary]:
    """Parse the invoice list endpoint; malformed items are skipped and logged."""
    invoices: list[InvoiceSummary] = []
    for index, item in enumerate(data):
        try:
            invoices.append(_parse_summary(item))
        except PARSE_ERRORS as err:
            _LOGGER.warning(
                "Dots-factuur op index %s overgeslagen: onverwacht formaat (%s)",
                index,
                type(err).__name__,
            )
    return invoices


def parse_invoice_detail(data: dict[str, Any]) -> InvoiceDetail:
    """Parse the invoice detail endpoint, without payment data."""
    lines: list[InvoiceLine] = [
        InvoiceLine(
            kind="charge",
            api_category=charge.get("category"),
            description=str(charge.get("description") or ""),
            quantity=_opt_dec(charge.get("quantity")),
            unit_price=_opt_dec(charge.get("unitPrice")),
            amount=_dec(charge.get("amount") or 0),
        )
        for charge in data.get("charges") or []
    ]
    lines.extend(
        InvoiceLine(
            kind="discount",
            api_category=None,
            description=str(discount.get("description") or ""),
            quantity=None,
            unit_price=_opt_dec(discount.get("discountValue")),
            amount=_dec(discount.get("amount") or 0),
        )
        for discount in data.get("discounts") or []
    )
    lines.extend(
        InvoiceLine(
            kind="tax",
            api_category=None,
            description=str(tax.get("taxType") or ""),
            quantity=_opt_dec(tax.get("taxableAmount")),
            unit_price=_opt_dec(tax.get("taxRate")),
            amount=_dec(tax.get("taxAmount") or 0),
        )
        for tax in data.get("taxes") or []
    )
    return InvoiceDetail(summary=_parse_summary(data), lines=tuple(lines))


def _str_or_none(value: Decimal | date | None) -> str | None:
    if value is None:
        return None
    return value.isoformat() if isinstance(value, date) else str(value)


def detail_to_dict(detail: InvoiceDetail) -> dict[str, Any]:
    """Serialise a detail to JSON-safe data for the Store."""
    s = detail.summary
    return {
        "summary": {
            "id": s.id,
            "number": s.number,
            "invoice_date": s.invoice_date.isoformat(),
            "due_date": _str_or_none(s.due_date),
            "period_start": s.period_start.isoformat(),
            "period_end": s.period_end.isoformat(),
            "energy_type": s.energy_type,
            "invoice_type": s.invoice_type,
            "total": str(s.total),
            "tax": str(s.tax),
            "net": str(s.net),
            "status": s.status,
            "updated_at": s.updated_at.isoformat(),
        },
        "lines": [
            {
                "kind": line.kind,
                "api_category": line.api_category,
                "description": line.description,
                "quantity": _str_or_none(line.quantity),
                "unit_price": _str_or_none(line.unit_price),
                "amount": str(line.amount),
            }
            for line in detail.lines
        ],
    }


def detail_from_dict(data: dict[str, Any]) -> InvoiceDetail:
    """Inverse of detail_to_dict."""
    s = data["summary"]
    summary = InvoiceSummary(
        id=int(s["id"]),
        number=s["number"],
        invoice_date=date.fromisoformat(s["invoice_date"]),
        due_date=_opt_date(s["due_date"]),
        period_start=date.fromisoformat(s["period_start"]),
        period_end=date.fromisoformat(s["period_end"]),
        energy_type=s["energy_type"],
        invoice_type=s["invoice_type"],
        total=Decimal(s["total"]),
        tax=Decimal(s["tax"]),
        net=Decimal(s["net"]),
        status=s["status"],
        updated_at=datetime.fromisoformat(s["updated_at"]),
    )
    lines = tuple(
        InvoiceLine(
            kind=line["kind"],
            api_category=line["api_category"],
            description=line["description"],
            quantity=_opt_dec(line["quantity"]),
            unit_price=_opt_dec(line["unit_price"]),
            amount=Decimal(line["amount"]),
        )
        for line in data["lines"]
    )
    return InvoiceDetail(summary=summary, lines=lines)

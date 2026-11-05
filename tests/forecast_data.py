"""Invented invoices and series for the forecast tests (no real customer data)."""

from __future__ import annotations

import calendar
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from custom_components.dots_energy.categories import Breakdown, breakdown
from custom_components.dots_energy.models import InvoiceDetail, parse_invoice_detail

CENT = Decimal("0.01")


def _r(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def invoice_dict(
    *,
    invoice_id: int = 1,
    period: str = "2026-01",
    kwh_in: float = 200,
    kwh_out: float = 50,
    energy_price: float = 0.12,
    injection_price: float = 0.10,
    transport: float = 38.0,
    flex: float = 4.0,
    invoice_type: str = "out_invoice",
) -> dict[str, Any]:
    """A monthly invoice shaped like the Dots API, with consistent totals."""
    year, month = (int(p) for p in period.split("-"))
    days = calendar.monthrange(year, month)[1]
    k_in, k_out = Decimal(str(kwh_in)), Decimal(str(kwh_out))
    mwh = (k_in / 1000).quantize(Decimal("0.001"))
    year_fraction = (Decimal(days) / 365).quantize(Decimal("0.001"))

    def charge(category: str, description: str, quantity: Decimal, unit: Decimal, amount=None):
        value = _r(quantity * unit) if amount is None else _r(Decimal(str(amount)))
        return {
            "category": category,
            "description": description,
            "quantity": float(quantity),
            "unit": "Units",
            "unitPrice": float(unit),
            "amount": float(value),
        }

    energy, net, tax = (
        "Energielevering elektriciteit",
        "Nettarieven elektriciteit",
        "Toeslagen elektriciteit",
    )
    charges = [
        charge(energy, "Elektriciteit totale uren", k_in, Decimal(str(energy_price))),
        charge(net, "Meet- en telactiviteit elektriciteit", Decimal(1), Decimal("0.05") * days),
        charge(net, "Transport en distributie elektriciteit", Decimal(1), Decimal(str(transport))),
        charge(energy, "Groenestroomcertificaten", mwh, Decimal("11")),
        charge(energy, "Warmtekrachtcertificaten", mwh, Decimal("4.6")),
        charge(tax, "Accijnzen elektriciteit", k_in, Decimal(0)),
        charge(tax, "Bijzondere accijns elektriciteit", k_in, Decimal("0.05")),
        charge(tax, "Vlaamse energieheffing elektriciteit", year_fraction, Decimal(0)),
        charge(energy, "Evenwichtsbijdrage injectie", k_out, Decimal("0.005")),
        charge(
            energy,
            "Vaste vergoeding elektriciteit",
            year_fraction,
            Decimal(120),
            amount=Decimal(120) * days / 365,
        ),
        charge(energy, "Evenwichtsbijdrage afname", k_in, Decimal("0.005")),
    ]
    settlement = _r(k_out * Decimal(str(injection_price)))
    discounts = [
        {
            "description": "Flexibiliteitsvergoeding",
            "discountType": "fixed",
            "discountValue": flex,
            "amount": -flex,
        },
        {
            "description": "Settlement injectie totale uren",
            "discountType": "fixed",
            "discountValue": injection_price,
            "amount": float(-settlement),
        },
    ]
    taxable = sum(
        (
            Decimal(str(c["amount"]))
            for c in charges
            if c["description"] != "Evenwichtsbijdrage injectie"
        ),
        Decimal(0),
    )
    vat = _r(taxable * Decimal("0.06"))
    net_amount = (
        sum((Decimal(str(c["amount"])) for c in charges), Decimal(0))
        - Decimal(str(flex))
        - settlement
    )
    end_month = month % 12 + 1
    end_year = year + (1 if month == 12 else 0)
    return {
        "id": invoice_id,
        "invoiceNumber": f"TST{invoice_id:08d}",
        "invoiceDate": f"{end_year}-{end_month:02d}-05",
        "dueDate": None,
        "periodStart": f"{year}-{month:02d}-01",
        "periodEnd": f"{end_year}-{end_month:02d}-01",
        "energyType": "electricity",
        "invoiceType": invoice_type,
        "totalAmount": float(net_amount + vat),
        "taxAmount": float(vat),
        "netAmount": float(net_amount),
        "currency": "EUR",
        "status": "paid",
        "updatedAt": f"{end_year}-{end_month:02d}-05T08:00:00",
        "charges": charges,
        "discounts": discounts,
        "taxes": [
            {
                "taxType": "6%",
                "taxRate": 6,
                "taxableAmount": float(taxable),
                "taxAmount": float(vat),
            }
        ],
    }


def detail(**kwargs: Any) -> InvoiceDetail:
    """An InvoiceDetail built from invoice_dict."""
    return parse_invoice_detail(invoice_dict(**kwargs))


def breakdowns(*details: InvoiceDetail) -> dict[int, Breakdown]:
    """Breakdowns keyed by invoice id."""
    return {d.summary.id: breakdown(d) for d in details}

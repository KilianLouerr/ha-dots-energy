"""In-memory stand-in for DotsApiClient."""

from __future__ import annotations

import copy
from typing import Any

from custom_components.dots_energy.api import ApiError
from custom_components.dots_energy.models import parse_invoice_detail, parse_invoice_list

from .conftest import load_fixture


def detail_for(summary: dict[str, Any]) -> dict[str, Any]:
    """A minimal detail whose single energy line equals the invoice total."""
    detail = copy.deepcopy(summary)
    detail["charges"] = [
        {
            "category": "Energielevering elektriciteit",
            "description": "Elektriciteit totale uren",
            "quantity": 100.0,
            "unitPrice": 0,
            "amount": summary["totalAmount"],
        }
    ]
    detail["discounts"] = []
    detail["taxes"] = []
    return detail


class FakeApi:
    """Serves invoices from fixtures; records detail calls."""

    def __init__(self) -> None:
        self.invoices: list[dict[str, Any]] = load_fixture("invoice_list.json")
        self.details: dict[int, dict[str, Any]] = {102: load_fixture("invoice_detail.json")}
        self.detail_calls: list[int] = []
        self.fail_details: set[int] = set()
        self.list_error: Exception | None = None

    def add_invoice(self, **overrides: Any) -> dict[str, Any]:
        invoice = copy.deepcopy(self.invoices[0]) | overrides
        self.invoices.append(invoice)
        return invoice

    async def async_get_invoices(self, location_id: int):
        if self.list_error is not None:
            raise self.list_error
        return parse_invoice_list(copy.deepcopy(self.invoices))

    async def async_get_invoice(self, invoice_id: int):
        self.detail_calls.append(invoice_id)
        if invoice_id in self.fail_details:
            raise ApiError("boom")
        if invoice_id in self.details:
            return parse_invoice_detail(copy.deepcopy(self.details[invoice_id]))
        summary = next(i for i in self.invoices if i["id"] == invoice_id)
        return parse_invoice_detail(detail_for(summary))

    async def async_get_active_contract(self, location_id: int):
        return {"pricingType": "Dynamic", "productName": "TEST_PRODUCT"}

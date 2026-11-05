"""Tests for invoice parsing."""

import json
from datetime import date, datetime
from decimal import Decimal

from custom_components.dots_energy.models import (
    detail_from_dict,
    detail_to_dict,
    parse_invoice_detail,
    parse_invoice_list,
)

from .conftest import load_fixture


def test_parse_list_types() -> None:
    invoices = parse_invoice_list(load_fixture("invoice_list.json"))

    assert [i.id for i in invoices] == [101, 102, 103]
    first = invoices[0]
    assert first.number == "TST00000101"
    assert first.total == Decimal("80.0")
    assert isinstance(first.total, Decimal)
    assert first.period_start == date(2026, 1, 1)
    assert first.period_end == date(2026, 2, 1)
    assert first.updated_at == datetime(2026, 2, 27, 9, 0)
    assert invoices[2].due_date is None
    assert invoices[2].invoice_type == "out_refund"
    assert invoices[2].total == Decimal("-12.5")


def test_parse_detail_drops_payment() -> None:
    detail = parse_invoice_detail(load_fixture("invoice_detail.json"))

    dumped = json.dumps(detail_to_dict(detail))
    for secret in ("BE00 0000", "+++000/0000/00000+++", "Testbank", "payment"):
        assert secret not in repr(detail)
        assert secret not in dumped


def test_detail_lines_in_order_and_signed() -> None:
    detail = parse_invoice_detail(load_fixture("invoice_detail.json"))

    kinds = [line.kind for line in detail.lines]
    assert kinds == ["charge"] * 11 + ["discount"] * 3 + ["tax"]
    energy = detail.lines[0]
    assert energy.api_category == "Energielevering elektriciteit"
    assert energy.quantity == Decimal("250.0")
    assert energy.unit_price == Decimal("0.12")
    assert energy.amount == Decimal("30.0")
    settlement = detail.lines[12]
    assert settlement.description == "Settlement injectie totale uren"
    assert settlement.amount == Decimal("-10.0")
    assert settlement.api_category is None


def test_tax_lines_use_tax_amount() -> None:
    detail = parse_invoice_detail(load_fixture("invoice_detail.json"))

    tax = detail.lines[-1]
    assert tax.kind == "tax"
    assert tax.description == "6%"
    assert tax.amount == Decimal("5.77")
    assert tax.quantity == Decimal("96.11")


def test_detail_roundtrip() -> None:
    detail = parse_invoice_detail(load_fixture("invoice_detail.json"))

    assert detail_from_dict(json.loads(json.dumps(detail_to_dict(detail)))) == detail


def test_parse_list_skips_malformed_items(caplog) -> None:
    data = load_fixture("invoice_list.json")
    data[1]["periodStart"] = None
    data[2]["totalAmount"] = "not-a-number"

    invoices = parse_invoice_list(data)

    assert [i.id for i in invoices] == [101]
    assert "index 1" in caplog.text
    assert "index 2" in caplog.text
    assert "TST0000" not in caplog.text

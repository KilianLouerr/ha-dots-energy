"""Tests for invoice categorisation."""

from __future__ import annotations

import dataclasses
import logging
from decimal import Decimal

import pytest

from custom_components.dots_energy.categories import (
    breakdown,
    breakdown_from_summary,
    categorize_line,
)
from custom_components.dots_energy.const import Category
from custom_components.dots_energy.models import (
    InvoiceLine,
    parse_invoice_detail,
    parse_invoice_list,
)

from .conftest import load_fixture

D = Decimal


def _detail():
    return parse_invoice_detail(load_fixture("invoice_detail.json"))


def test_fixture_breakdown_sums_exactly_to_total() -> None:
    result = breakdown(_detail())

    assert result.amounts == {
        Category.ENERGIE: D("35.65"),
        Category.NETKOSTEN: D("39.50"),
        Category.HEFFINGEN: D("11.50"),
        Category.VASTE_KOSTEN: D("9.96"),
        Category.BTW: D("5.77"),
        Category.INJECTIE: D("-10.00"),
        Category.VERGOEDINGEN: D("-4.00"),
        Category.OVERIG: D("0.00"),
    }
    assert sum(result.amounts.values()) == D("88.38")
    assert result.deviation == D("0.00")


def _line(kind, description, api_category=None, amount="1"):
    return InvoiceLine(kind, api_category, description, None, None, D(amount))


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (_line("tax", "6%"), Category.BTW),
        (_line("discount", "Settlement injectie totale uren"), Category.INJECTIE),
        (_line("discount", "Afrondingsverschil negatief"), None),
        (_line("discount", "Afrondingsverschil positief"), None),
        (_line("charge", "Afrondingsverschil positief", "Diversen"), None),
        (_line("discount", "Flexibiliteitsvergoeding"), Category.VERGOEDINGEN),
        (
            _line("charge", "Vaste vergoeding elektriciteit", "Energielevering elektriciteit"),
            Category.VASTE_KOSTEN,
        ),
        (
            _line("charge", "Transport en distributie elektriciteit", "Nettarieven elektriciteit"),
            Category.NETKOSTEN,
        ),
        (
            _line("charge", "Bijzondere accijns elektriciteit", "Toeslagen elektriciteit"),
            Category.HEFFINGEN,
        ),
        (
            _line("charge", "Groenestroomcertificaten", "Energielevering elektriciteit"),
            Category.ENERGIE,
        ),
        (_line("charge", "Iets nieuws", "Nieuwe categorie"), Category.OVERIG),
    ],
)
def test_each_rule(line, expected) -> None:
    assert categorize_line(line) == expected


def test_unknown_line_goes_to_overig_and_warns_once(caplog) -> None:
    caplog.set_level(logging.WARNING)
    detail = _detail()
    extra = _line("charge", "Mysterieuze toeslag", "Onbekend", "2.00")
    summary = dataclasses.replace(detail.summary, total=detail.summary.total + D("2.00"))
    detail = dataclasses.replace(detail, summary=summary, lines=detail.lines + (extra,))

    first = breakdown(detail)
    breakdown(detail)

    assert first.amounts[Category.OVERIG] == D("2.00")
    assert first.deviation == D("0.00")
    assert sum(first.amounts.values()) == summary.total
    assert caplog.text.count("Mysterieuze toeslag") == 1


def test_deviation_over_5_cent_recorded(caplog) -> None:
    caplog.set_level(logging.WARNING)
    detail = _detail()
    summary = dataclasses.replace(detail.summary, total=detail.summary.total + D("0.10"))
    detail = dataclasses.replace(detail, summary=summary)

    result = breakdown(detail)

    assert result.amounts[Category.OVERIG] == D("0.10")
    assert result.deviation == D("0.10")
    assert sum(result.amounts.values()) == summary.total
    assert "afwijking" in caplog.text.lower()
    assert "TST00000102" not in caplog.text


def test_breakdown_from_summary_puts_everything_in_overig() -> None:
    summary = parse_invoice_list(load_fixture("invoice_list.json"))[0]

    result = breakdown_from_summary(summary)

    assert result.amounts[Category.OVERIG] == D("80.0")
    assert sum(result.amounts.values()) == D("80.0")
    assert result.deviation == D("0")
    assert set(result.amounts) == set(Category)

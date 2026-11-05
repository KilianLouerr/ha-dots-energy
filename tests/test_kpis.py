"""Tests for headline figures and the monthly overview."""

from __future__ import annotations

import dataclasses
from datetime import date
from decimal import Decimal

from custom_components.dots_energy.kpis import afname_kwh, compute_kpis, monthly_overview
from custom_components.dots_energy.models import parse_invoice_detail, parse_invoice_list

from .conftest import load_fixture

D = Decimal


def _data():
    summaries = parse_invoice_list(load_fixture("invoice_list.json"))
    detail = parse_invoice_detail(load_fixture("invoice_detail.json"))
    return summaries, {102: detail}


def test_afname_kwh() -> None:
    assert afname_kwh(_data()[1][102]) == D("250.0")


def test_kpis_definitions() -> None:
    summaries, details = _data()

    kpis = compute_kpis(summaries, details, today=date(2026, 3, 30))

    assert kpis.last_invoice.id == 102
    assert kpis.total_this_year == D("155.88")
    assert kpis.average_per_month == D("77.94")
    assert kpis.all_in_price == D("0.354")
    assert kpis.outstanding == D("88.38")


def test_kpis_calendar_year_and_unknown_status() -> None:
    summaries, details = _data()
    summaries[0] = dataclasses.replace(summaries[0], status="overdue")

    kpis = compute_kpis(summaries, details, today=date(2027, 1, 15))

    assert kpis.total_this_year == D("0")
    assert kpis.outstanding == D("168.38")


def test_kpis_average_only_last_12_months() -> None:
    summaries, details = _data()

    kpis = compute_kpis(summaries, details, today=date(2027, 1, 15))

    assert kpis.average_per_month == D("75.88")


def test_kpis_empty() -> None:
    kpis = compute_kpis([], {}, today=date(2026, 3, 30))

    assert kpis.last_invoice is None
    assert kpis.total_this_year == D("0")
    assert kpis.average_per_month is None
    assert kpis.all_in_price is None
    assert kpis.outstanding == D("0")


def test_credit_note_subtracts_in_same_month() -> None:
    summaries, details = _data()

    months = monthly_overview(summaries, details)

    assert [m["maand"] for m in months] == ["2026-01", "2026-02"]
    assert months[0]["totaal"] == 80.0
    assert months[0]["overig"] == 80.0
    feb = months[1]
    assert feb["totaal"] == 75.88
    assert feb["energie"] == 35.65
    assert feb["netkosten"] == 39.5
    assert feb["injectie"] == -10.0
    assert feb["overig"] == -12.5
    assert round(sum(v for k, v in feb.items() if k not in ("maand", "totaal")), 2) == 75.88


def test_kpis_warn_once_for_unknown_status_and_type(caplog) -> None:
    summaries, details = _data()
    summaries[0] = dataclasses.replace(summaries[0], status="overdue", invoice_type="deposit")

    compute_kpis(summaries, details, today=date(2026, 3, 30))
    compute_kpis(summaries, details, today=date(2026, 3, 30))

    assert caplog.text.count("overdue") == 1
    assert caplog.text.count("deposit") == 1
    assert "TST0000" not in caplog.text

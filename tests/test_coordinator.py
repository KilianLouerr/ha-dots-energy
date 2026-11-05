"""Tests for the update coordinator."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from custom_components.dots_energy.api import ApiAuthError, ApiError
from custom_components.dots_energy.auth import CannotConnect, CaptchaRequired
from custom_components.dots_energy.const import DOMAIN, EVENT_NEW_INVOICE, Category
from custom_components.dots_energy.coordinator import DotsCoordinator

from .fakes import FakeApi


@pytest.fixture(autouse=True)
def no_statistics():
    with patch("custom_components.dots_energy.coordinator.async_import_statistics") as mock:
        yield mock


@pytest.fixture
def entry(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"location_id": 7}, entry_id="entry1")
    entry.add_to_hass(hass)
    return entry


async def _coordinator(hass, entry, api) -> DotsCoordinator:
    coordinator = DotsCoordinator(hass, entry, api, 7)
    await coordinator.async_refresh()
    return coordinator


async def test_first_sync_backfills_without_events(hass, entry, no_statistics) -> None:
    events = async_capture_events(hass, EVENT_NEW_INVOICE)
    api = FakeApi()

    coordinator = await _coordinator(hass, entry, api)

    assert coordinator.last_update_success
    data = coordinator.data
    assert [s.id for s in data.summaries] == [101, 102, 103]
    assert set(data.details) == {101, 102, 103}
    assert data.breakdowns[102].amounts[Category.ENERGIE] == Decimal("35.65")
    assert data.kpis.last_invoice.id == 102
    assert [m["maand"] for m in data.monthly] == ["2026-01", "2026-02"]
    assert coordinator.selected_invoice_id == 102
    assert events == []
    no_statistics.assert_awaited_once()


async def test_new_invoice_fires_one_event_with_difference(hass, entry) -> None:
    events = async_capture_events(hass, EVENT_NEW_INVOICE)
    api = FakeApi()
    coordinator = await _coordinator(hass, entry, api)
    api.add_invoice(
        id=104,
        invoiceNumber="TST00000104",
        periodStart="2026-03-01",
        periodEnd="2026-04-01",
        invoiceDate="2026-04-20",
        totalAmount=90.0,
        status="pending",
        updatedAt="2026-04-20T08:00:00",
    )

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data == {
        "invoice_number": "TST00000104",
        "period": "2026-03",
        "amount": 90.0,
        "previous_amount": 88.38,
        "difference": 1.62,
    }
    assert coordinator.selected_invoice_id == 102


async def test_restart_does_not_refire(hass, entry) -> None:
    events = async_capture_events(hass, EVENT_NEW_INVOICE)
    api = FakeApi()
    first = await _coordinator(hass, entry, api)
    await first.async_flush_store()

    second = DotsCoordinator(hass, entry, api, 7)
    await second.async_refresh()
    await hass.async_block_till_done()

    assert events == []
    assert second.data.details.keys() == {101, 102, 103}


async def test_detail_only_fetched_when_updated_at_changes(hass, entry) -> None:
    api = FakeApi()
    coordinator = await _coordinator(hass, entry, api)
    assert sorted(api.detail_calls) == [101, 102, 103]
    api.detail_calls.clear()

    await coordinator.async_refresh()
    assert api.detail_calls == []

    api.invoices[1]["updatedAt"] = "2026-04-01T08:00:00"
    api.invoices[1]["status"] = "paid"
    await coordinator.async_refresh()
    assert api.detail_calls == [102]
    assert coordinator.data.kpis.outstanding == Decimal("0")


async def test_detail_failure_falls_back_to_summary(hass, entry) -> None:
    api = FakeApi()
    api.fail_details = {102}

    coordinator = await _coordinator(hass, entry, api)

    assert coordinator.last_update_success
    assert coordinator.data.breakdowns[102].amounts[Category.OVERIG] == Decimal("88.38")
    assert 102 not in coordinator.data.details

    api.fail_details = set()
    api.detail_calls.clear()
    await coordinator.async_refresh()
    assert api.detail_calls == [102]
    assert coordinator.data.breakdowns[102].amounts[Category.ENERGIE] == Decimal("35.65")


@pytest.mark.parametrize("error", [ApiAuthError("401"), CaptchaRequired("captcha")])
async def test_auth_failure_raises_config_entry_auth_failed_and_keeps_data(
    hass, entry, error
) -> None:
    api = FakeApi()
    coordinator = await _coordinator(hass, entry, api)
    previous = coordinator.data

    api.list_error = error
    await coordinator.async_refresh()

    assert not coordinator.last_update_success
    assert isinstance(coordinator.last_exception, ConfigEntryAuthFailed)
    assert coordinator.data is previous


@pytest.mark.parametrize("error", [ApiError("500"), CannotConnect("down")])
async def test_connection_failure_raises_update_failed_and_keeps_data(hass, entry, error) -> None:
    api = FakeApi()
    coordinator = await _coordinator(hass, entry, api)
    previous = coordinator.data

    api.list_error = error
    await coordinator.async_refresh()

    assert isinstance(coordinator.last_exception, UpdateFailed)
    assert not isinstance(coordinator.last_exception, ConfigEntryAuthFailed)
    assert coordinator.data is previous

"""Fetch Dots invoices periodically and derive everything the entities show."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import ApiAuthError, ApiError
from .auth import AuthError, CannotConnect
from .categories import Breakdown, breakdown, breakdown_from_summary
from .const import DOMAIN, EVENT_NEW_INVOICE, UPDATE_INTERVAL
from .kpis import Kpis, compute_kpis, is_credit_note, monthly_overview
from .models import InvoiceDetail, InvoiceSummary, detail_from_dict, detail_to_dict
from .statistics import async_import_statistics

if TYPE_CHECKING:
    from .api import DotsApiClient

_LOGGER = logging.getLogger(__name__)
STORAGE_VERSION = 1


def new_invoice_signal(entry_id: str) -> str:
    """Dispatcher signal fired for each new invoice."""
    return f"{DOMAIN}_new_invoice_{entry_id}"


@dataclass
class DotsData:
    """Everything derived from one successful synchronisation."""

    summaries: list[InvoiceSummary]
    details: dict[int, InvoiceDetail]
    breakdowns: dict[int, Breakdown]
    kpis: Kpis
    monthly: list[dict[str, Any]]
    contract: dict[str, Any]
    last_success: datetime
    by_id: dict[int, InvoiceSummary] = field(init=False)

    def __post_init__(self) -> None:
        self.by_id = {s.id: s for s in self.summaries}


class DotsCoordinator(DataUpdateCoordinator[DotsData]):
    """Polls the invoice list and caches invoice details in a Store."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        api: DotsApiClient,
        location_id: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self._api = api
        self._location_id = location_id
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        self._stored: dict[str, Any] | None = None
        self._contract: dict[str, Any] = {}
        self.selected_invoice_id: int | None = None

    async def _async_load_store(self) -> dict[str, Any]:
        if self._stored is None:
            loaded = await self._store.async_load() or {}
            self._stored = {
                "synced": bool(loaded.get("synced")),
                "known_ids": list(loaded.get("known_ids", [])),
                "details": dict(loaded.get("details", {})),
                "updated_at": dict(loaded.get("updated_at", {})),
            }
        return self._stored

    async def async_flush_store(self) -> None:
        """Persist the cache now."""
        if self._stored is not None:
            await self._store.async_save(self._stored)

    async def _async_update_data(self) -> DotsData:
        stored = await self._async_load_store()
        try:
            summaries = await self._api.async_get_invoices(self._location_id)
            try:
                self._contract = await self._api.async_get_active_contract(self._location_id)
            except ApiError as err:
                if isinstance(err, ApiAuthError):
                    raise
                _LOGGER.debug("Contract not refreshed: %s", err)
            details = await self._async_fetch_details(summaries, stored)
        except (ApiAuthError, AuthError) as err:
            if isinstance(err, CannotConnect):
                raise UpdateFailed(f"Dots niet bereikbaar: {err}") from err
            raise ConfigEntryAuthFailed(f"Aanmelden bij Dots mislukt: {err}") from err
        except ApiError as err:
            raise UpdateFailed(f"Dots-API fout: {err}") from err

        self._fire_new_invoice_events(summaries, stored)
        await self.async_flush_store()

        breakdowns = {
            s.id: breakdown(details[s.id]) if s.id in details else breakdown_from_summary(s)
            for s in summaries
        }
        kpis = compute_kpis(summaries, details, dt_util.now().date())
        monthly = monthly_overview(summaries, details)
        ids = {s.id for s in summaries}
        if self.selected_invoice_id not in ids:
            self.selected_invoice_id = kpis.last_invoice.id if kpis.last_invoice else None
        await async_import_statistics(self.hass, monthly)
        return DotsData(
            summaries=summaries,
            details=details,
            breakdowns=breakdowns,
            kpis=kpis,
            monthly=monthly,
            contract=self._contract,
            last_success=dt_util.utcnow(),
        )

    async def _async_fetch_details(
        self, summaries: list[InvoiceSummary], stored: dict[str, Any]
    ) -> dict[int, InvoiceDetail]:
        cached: dict[str, Any] = stored["details"]
        updated: dict[str, str] = stored["updated_at"]
        current = {str(s.id) for s in summaries}
        for key in [k for k in cached if k not in current]:
            cached.pop(key, None)
            updated.pop(key, None)
        details: dict[int, InvoiceDetail] = {}
        for summary in summaries:
            key = str(summary.id)
            stamp = summary.updated_at.isoformat()
            if key in cached and updated.get(key) == stamp:
                details[summary.id] = detail_from_dict(cached[key])
                continue
            try:
                detail = await self._api.async_get_invoice(summary.id)
            except ApiError as err:
                if isinstance(err, ApiAuthError):
                    raise
                _LOGGER.warning(
                    "Details van Dots-factuur (periode %s) niet opgehaald; later opnieuw: %s",
                    summary.period_start.isoformat(),
                    err,
                )
                if key in cached:
                    details[summary.id] = detail_from_dict(cached[key])
                continue
            cached[key] = detail_to_dict(detail)
            updated[key] = stamp
            details[summary.id] = detail
        return details

    def _fire_new_invoice_events(
        self, summaries: list[InvoiceSummary], stored: dict[str, Any]
    ) -> None:
        known = set(stored["known_ids"])
        if stored["synced"]:
            regular = sorted(
                (s for s in summaries if not is_credit_note(s)),
                key=lambda s: (s.period_start, s.invoice_date, s.id),
            )
            new = sorted(
                (s for s in summaries if s.id not in known),
                key=lambda s: (s.period_start, s.invoice_date, s.id),
            )
            for summary in new:
                previous = next(
                    (
                        p
                        for p in reversed(regular)
                        if p.id != summary.id
                        and (p.period_start, p.invoice_date, p.id)
                        < (summary.period_start, summary.invoice_date, summary.id)
                    ),
                    None,
                )
                payload = {
                    "invoice_number": summary.number,
                    "period": summary.period_start.strftime("%Y-%m"),
                    "amount": float(summary.total),
                    "previous_amount": float(previous.total) if previous else None,
                    "difference": float(summary.total - previous.total) if previous else None,
                }
                self.hass.bus.async_fire(EVENT_NEW_INVOICE, payload)
                async_dispatcher_send(
                    self.hass, new_invoice_signal(self.config_entry.entry_id), payload
                )
        stored["known_ids"] = sorted(known | {s.id for s in summaries})
        stored["synced"] = True

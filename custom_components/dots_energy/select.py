"""Select one Dots invoice to show its lines."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DotsConfigEntry
from .entity import DotsEntity
from .kpis import invoice_label, newest_first


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DotsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the invoice select."""
    async_add_entities([DotsInvoiceSelect(entry.runtime_data)])


class DotsInvoiceSelect(DotsEntity, SelectEntity):
    """Choose which invoice the 'gekozen factuur' sensor shows."""

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "select", "factuur")

    @property
    def options(self) -> list[str]:
        """All invoices, newest first."""
        return [invoice_label(s) for s in newest_first(self.coordinator.data.summaries)]

    @property
    def current_option(self) -> str | None:
        """The selected invoice."""
        selected = self.coordinator.data.by_id.get(self.coordinator.selected_invoice_id)
        return invoice_label(selected) if selected else None

    async def async_select_option(self, option: str) -> None:
        """Select an invoice; no API call is needed."""
        for summary in self.coordinator.data.summaries:
            if invoice_label(summary) == option:
                self.coordinator.selected_invoice_id = summary.id
                self.coordinator.async_update_listeners()
                return

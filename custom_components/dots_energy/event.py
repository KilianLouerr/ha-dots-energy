"""Event entity fired when Dots issues a new invoice."""

from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DotsConfigEntry
from .coordinator import new_invoice_signal
from .entity import DotsEntity

EVENT_TYPE = "nieuwe_factuur"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DotsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the new-invoice event entity."""
    async_add_entities([DotsNewInvoiceEvent(entry.runtime_data)])


class DotsNewInvoiceEvent(DotsEntity, EventEntity):
    """Triggers once per new invoice."""

    _attr_event_types = [EVENT_TYPE]

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "event", "nieuwe_factuur")

    async def async_added_to_hass(self) -> None:
        """Listen for new invoices from the coordinator."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                new_invoice_signal(self.coordinator.config_entry.entry_id),
                self._handle_new_invoice,
            )
        )

    @callback
    def _handle_new_invoice(self, payload: dict[str, Any]) -> None:
        self._trigger_event(EVENT_TYPE, payload)
        self.async_write_ha_state()

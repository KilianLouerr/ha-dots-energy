"""Shared base for Dots Energy entities."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import DotsCoordinator


class DotsEntity(CoordinatorEntity[DotsCoordinator]):
    """Entity bound to the Dots coordinator.

    Entities keep showing the last synchronised data when an update fails
    (for example while Dots asks to sign in again); the "laatst bijgewerkt"
    sensor and the reauth notification make the staleness visible.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: DotsCoordinator, platform: str, key: str) -> None:
        super().__init__(coordinator)
        entry_id = coordinator.config_entry.entry_id
        self._attr_unique_id = f"{entry_id}_{key}"
        self._attr_translation_key = key
        self.entity_id = f"{platform}.{DOMAIN}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="Dots Energy",
            manufacturer="Dots Energy",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def available(self) -> bool:
        """Available as long as there is data from one successful sync."""
        return self.coordinator.data is not None

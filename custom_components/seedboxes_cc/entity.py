"""Base entities for the Seedboxes.cc integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, NAME, VERSION
from .coordinator import SeedboxDataUpdateCoordinator


class SeedboxBaseEntity(CoordinatorEntity[SeedboxDataUpdateCoordinator]):
    """Base entity linked to the Seedboxes.cc coordinator."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: SeedboxDataUpdateCoordinator,
        config_entry: ConfigEntry,
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._config_entry = config_entry

    @property
    def device_info(self) -> DeviceInfo:
        """Return information about the Seedboxes.cc device."""
        seedbox_id = str(self._config_entry.data.get("seedbox_id", self._config_entry.entry_id))
        return DeviceInfo(
            identifiers={(DOMAIN, seedbox_id)},
            name=f"{NAME} {seedbox_id}",
            manufacturer="Seedboxes.cc",
            model="Seedbox",
            sw_version=VERSION,
            configuration_url=f"https://seedboxes.cc/dashboard/seedboxes/{seedbox_id}",
        )

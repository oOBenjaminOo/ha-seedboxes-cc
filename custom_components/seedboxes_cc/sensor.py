"""Sensor platform for the Seedboxes.cc integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfInformation
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    NAME_DISK_QUOTA_FREE,
    NAME_DISK_QUOTA_USED,
    NAME_DISK_QUOTA_USED_PCT,
    NAME_DISK_SIZE,
    NAME_IP_ADDRESS,
    NAME_MONTHLY_TRAFFIC,
    NAME_STATUS,
    NAME_TORRENT_CLIENT,
)
from .coordinator import SeedboxDataUpdateCoordinator
from .entity import SeedboxBaseEntity


@dataclass(frozen=True, kw_only=True)
class SeedboxSensorEntityDescription(SensorEntityDescription):
    """Describe a Seedboxes.cc sensor."""

    data_key: str


SENSORS: tuple[SeedboxSensorEntityDescription, ...] = (
    SeedboxSensorEntityDescription(key="status", translation_key="status", data_key=NAME_STATUS, icon="mdi:cloud-check"),
    SeedboxSensorEntityDescription(key="disk_free", translation_key="disk_free", data_key=NAME_DISK_QUOTA_FREE, native_unit_of_measurement=UnitOfInformation.GIGABYTES, device_class=SensorDeviceClass.DATA_SIZE, state_class=SensorStateClass.MEASUREMENT, icon="mdi:harddisk"),
    SeedboxSensorEntityDescription(key="disk_used", translation_key="disk_used", data_key=NAME_DISK_QUOTA_USED, native_unit_of_measurement=UnitOfInformation.GIGABYTES, device_class=SensorDeviceClass.DATA_SIZE, state_class=SensorStateClass.MEASUREMENT, icon="mdi:harddisk"),
    SeedboxSensorEntityDescription(key="disk_used_percent", translation_key="disk_used_percent", data_key=NAME_DISK_QUOTA_USED_PCT, native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT, icon="mdi:chart-donut"),
    SeedboxSensorEntityDescription(key="monthly_traffic", translation_key="monthly_traffic", data_key=NAME_MONTHLY_TRAFFIC, native_unit_of_measurement=UnitOfInformation.GIGABYTES, device_class=SensorDeviceClass.DATA_SIZE, state_class=SensorStateClass.TOTAL_INCREASING, icon="mdi:upload-network"),
    SeedboxSensorEntityDescription(key="disk_size", translation_key="disk_size", data_key=NAME_DISK_SIZE, native_unit_of_measurement=UnitOfInformation.GIGABYTES, device_class=SensorDeviceClass.DATA_SIZE, icon="mdi:database"),
    SeedboxSensorEntityDescription(key="ip_address", translation_key="ip_address", data_key=NAME_IP_ADDRESS, icon="mdi:ip-network"),
    SeedboxSensorEntityDescription(key="torrent_client", translation_key="torrent_client", data_key=NAME_TORRENT_CLIENT, icon="mdi:download-network"),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Seedboxes.cc sensors from a config entry."""
    coordinator: SeedboxDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        SeedboxSensor(coordinator, entry, description) for description in SENSORS
    )


class SeedboxSensor(SeedboxBaseEntity, SensorEntity):
    """Representation of a Seedboxes.cc sensor."""

    entity_description: SeedboxSensorEntityDescription

    def __init__(self, coordinator: SeedboxDataUpdateCoordinator, entry: ConfigEntry, description: SeedboxSensorEntityDescription) -> None:
        super().__init__(coordinator, entry)
        self.entity_description = description
        seedbox_id = str(entry.data.get("seedbox_id", entry.entry_id))
        self._attr_unique_id = f"{seedbox_id}_{description.key}"

    @property
    def native_value(self) -> Any:
        """Return the latest sensor value."""
        return self.coordinator.data.get(self.entity_description.data_key)

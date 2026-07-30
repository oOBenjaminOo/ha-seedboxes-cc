"""Seedboxes.cc integration for Home Assistant."""

from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_SCAN_PERIOD,
    CONF_SEEDBOX_ID,
    CONF_SESSION_COOKIE,
    DEFAULT_SCAN_PERIOD,
    DOMAIN,
    PLATFORMS,
    STARTUP_MESSAGE,
)
from .coordinator import SeedboxDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Seedboxes.cc from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    if len(hass.data[DOMAIN]) == 0:
        _LOGGER.info(STARTUP_MESSAGE)

    scan_period = timedelta(
        seconds=entry.options.get(CONF_SCAN_PERIOD, DEFAULT_SCAN_PERIOD)
    )

    coordinator = SeedboxDataUpdateCoordinator(
        hass,
        entry.data[CONF_SEEDBOX_ID],
        entry.data[CONF_SESSION_COOKIE],
        scan_period,
    )
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Seedboxes.cc config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id)
        if not hass.data[DOMAIN]:
            hass.data.pop(DOMAIN)

    return unloaded


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload a Seedboxes.cc config entry."""
    await hass.config_entries.async_reload(entry.entry_id)

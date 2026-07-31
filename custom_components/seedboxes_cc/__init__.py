"""Seedboxes.cc integration for Home Assistant."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry, ConfigEntryAuthFailed
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .const import (
    CONF_SCAN_PERIOD,
    CONF_SEEDBOX_ID,
    CONF_SESSION_COOKIE,
    CONFIG_ENTRY_MINOR_VERSION,
    CONFIG_ENTRY_VERSION,
    DEFAULT_SCAN_PERIOD,
    DOMAIN,
    PLATFORMS,
    STARTUP_MESSAGE,
)
from .coordinator import SeedboxDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_migrate_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> bool:
    """Migrate compatible Seedboxes.cc config entries."""
    if entry.version == CONFIG_ENTRY_VERSION:
        if entry.minor_version < CONFIG_ENTRY_MINOR_VERSION:
            hass.config_entries.async_update_entry(
                entry,
                minor_version=CONFIG_ENTRY_MINOR_VERSION,
            )
        return True

    if not 2 <= entry.version < CONFIG_ENTRY_VERSION:
        _LOGGER.error(
            "Cannot migrate Seedboxes.cc config entry %s from unsupported "
            "version %s; remove and add the integration again",
            entry.entry_id,
            entry.version,
        )
        return False

    data = entry.data
    has_seedbox_id = bool(str(data.get(CONF_SEEDBOX_ID, "")).strip())
    has_session_cookie = bool(str(data.get(CONF_SESSION_COOKIE, "")).strip())
    has_account_credentials = bool(
        str(data.get(CONF_USERNAME, "")).strip() and data.get(CONF_PASSWORD)
    )
    if not has_seedbox_id or not (has_session_cookie or has_account_credentials):
        _LOGGER.error(
            "Cannot migrate Seedboxes.cc config entry %s because its "
            "authentication data is incomplete; remove and add the "
            "integration again",
            entry.entry_id,
        )
        return False

    _LOGGER.info(
        "Migrating Seedboxes.cc config entry %s from version %s to %s",
        entry.entry_id,
        entry.version,
        CONFIG_ENTRY_VERSION,
    )
    hass.config_entries.async_update_entry(
        entry,
        version=CONFIG_ENTRY_VERSION,
        minor_version=CONFIG_ENTRY_MINOR_VERSION,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Seedboxes.cc from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    if len(hass.data[DOMAIN]) == 0:
        _LOGGER.info(STARTUP_MESSAGE)

    username = entry.data.get(CONF_USERNAME)
    password = entry.data.get(CONF_PASSWORD)
    session_cookie = entry.data.get(CONF_SESSION_COOKIE)
    if not session_cookie and (not username or not password):
        raise ConfigEntryAuthFailed(
            "Account credentials or a session cookie are required; "
            "reauthenticate the integration"
        )

    scan_period = timedelta(
        seconds=entry.options.get(CONF_SCAN_PERIOD, DEFAULT_SCAN_PERIOD)
    )

    coordinator = SeedboxDataUpdateCoordinator(
        hass,
        entry,
        scan_period,
    )
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Seedboxes.cc config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id)
        if not hass.data[DOMAIN]:
            hass.data.pop(DOMAIN)

    return unloaded

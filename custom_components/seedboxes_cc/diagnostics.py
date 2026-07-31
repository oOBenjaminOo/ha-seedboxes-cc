"""Diagnostics support for the Seedboxes.cc integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .const import CONF_SEEDBOX_ID, CONF_SESSION_COOKIE, DOMAIN, NAME_IP_ADDRESS

TO_REDACT = {
    CONF_PASSWORD,
    CONF_SEEDBOX_ID,
    CONF_SESSION_COOKIE,
    CONF_USERNAME,
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a Seedboxes.cc config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]

    return {
        "config_entry": {
            "title": "**REDACTED**",
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
            "version": entry.version,
        },
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "data": async_redact_data(
                dict(coordinator.data or {}),
                {NAME_IP_ADDRESS},
            ),
        },
    }

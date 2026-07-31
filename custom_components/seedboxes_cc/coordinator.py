"""Data update coordinator for the Seedboxes.cc integration."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryAuthFailed
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import CONF_SEEDBOX_ID, CONF_SESSION_COOKIE, DOMAIN
from .seedbox_client import (
    HybridSeedboxClient,
    SeedboxAuthenticationError,
    SeedboxBrowserVerificationRequired,
    SeedboxDataError,
)

_LOGGER = logging.getLogger(__name__)


class SeedboxDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinate Seedboxes.cc data updates."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        scan_period: timedelta,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=scan_period,
            config_entry=entry,
        )
        self._entry = entry
        self._persisted_cookie = entry.data.get(CONF_SESSION_COOKIE)
        self.api = HybridSeedboxClient(
            async_get_clientsession(hass),
            entry.data[CONF_SEEDBOX_ID],
            entry.data.get(CONF_USERNAME),
            entry.data.get(CONF_PASSWORD),
            self._persisted_cookie,
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch the latest Seedboxes.cc data."""
        try:
            result = await self.api.async_get_data()
        except (
            SeedboxAuthenticationError,
            SeedboxBrowserVerificationRequired,
        ) as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except SeedboxDataError as err:
            raise UpdateFailed(str(err)) from err
        except Exception as err:
            raise UpdateFailed(f"Unable to update Seedboxes.cc data: {err}") from err

        self._async_persist_rotated_cookie()
        return result.get("data", {})

    def _async_persist_rotated_cookie(self) -> None:
        """Persist a validated rotated cookie without overwriting newer data."""
        session_cookie = self.api.session_cookie
        if not session_cookie or session_cookie == self._persisted_cookie:
            return

        current_cookie = self._entry.data.get(CONF_SESSION_COOKIE)
        if current_cookie != self._persisted_cookie:
            return

        self.hass.config_entries.async_update_entry(
            self._entry,
            data={
                **self._entry.data,
                CONF_SESSION_COOKIE: session_cookie,
            },
        )
        self._persisted_cookie = session_cookie

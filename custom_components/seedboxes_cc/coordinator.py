"""Data update coordinator for the Seedboxes.cc integration."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN
from .seedbox_client import SeedboxClient

_LOGGER = logging.getLogger(__name__)


class SeedboxDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinate Seedboxes.cc data updates."""

    def __init__(
        self,
        hass: HomeAssistant,
        seedbox_id: str,
        session_cookie: str,
        scan_period: timedelta,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=scan_period,
        )
        self.api = SeedboxClient(
            async_get_clientsession(hass),
            seedbox_id,
            session_cookie,
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch the latest Seedboxes.cc data."""
        try:
            result = await self.api.async_get_data()
        except Exception as err:
            raise UpdateFailed(f"Unable to update Seedboxes.cc data: {err}") from err

        return result.get("data", {})

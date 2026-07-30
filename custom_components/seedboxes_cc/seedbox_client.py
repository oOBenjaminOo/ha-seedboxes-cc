"""Client for the Seedboxes.cc dashboard."""

from __future__ import annotations

import re
from typing import Any

import aiohttp

from .const import (
    NAME_DISK_QUOTA_FREE,
    NAME_DISK_QUOTA_USED,
    NAME_DISK_QUOTA_USED_PCT,
    NAME_DISK_SIZE,
    NAME_IP_ADDRESS,
    NAME_MONTHLY_TRAFFIC,
    NAME_STATUS,
    NAME_TORRENT_CLIENT,
)

BASE_URL = "https://seedboxes.cc"


class SeedboxAuthenticationError(Exception):
    """Raised when the Seedboxes.cc session is invalid."""


class SeedboxDataError(Exception):
    """Raised when dashboard data cannot be parsed."""


class SeedboxClient:
    """Retrieve seedbox information from the authenticated dashboard page."""

    def __init__(self, session: aiohttp.ClientSession, seedbox_id: str, session_cookie: str) -> None:
        self._session = session
        self._seedbox_id = str(seedbox_id)
        self._session_cookie = session_cookie.strip()

    async def async_get_data(self) -> dict[str, Any]:
        """Fetch and parse the Seedboxes.cc dashboard."""
        url = f"{BASE_URL}/dashboard/seedboxes/{self._seedbox_id}"
        headers = {
            "Cookie": f"session_id={self._session_cookie}",
            "User-Agent": "HomeAssistant Seedboxes.cc Integration/2.0",
        }

        async with self._session.get(url, headers=headers, allow_redirects=False) as response:
            if response.status in (301, 302, 303, 307, 308, 401, 403):
                raise SeedboxAuthenticationError("Session cookie is invalid or expired")
            if response.status != 200:
                raise SeedboxDataError(f"Dashboard returned HTTP {response.status}")
            html = await response.text()

        if f'"seedboxId":"{self._seedbox_id}"' not in html:
            raise SeedboxAuthenticationError("The requested seedbox is not available in this session")

        disk_size = float(self._extract_number(html, "diskSpaceLimit"))
        traffic_raw = float(self._extract_number(html, "currentMonthTraffic"))
        metrics = re.findall(r'\\"diskspace\\":(\d+),\\"traffic\\":(\d+)', html)
        if not metrics:
            raise SeedboxDataError("No telemetry metrics found in dashboard page")

        disk_used_mb = float(metrics[-1][0])
        disk_used_gb = round(disk_used_mb / 1000, 2)
        disk_free_gb = round(max(disk_size - disk_used_gb, 0), 2)
        disk_used_pct = round((disk_used_gb / disk_size) * 100, 2) if disk_size else 0

        return {
            "data": {
                NAME_DISK_QUOTA_FREE: disk_free_gb,
                NAME_DISK_QUOTA_USED: disk_used_gb,
                NAME_DISK_QUOTA_USED_PCT: disk_used_pct,
                NAME_MONTHLY_TRAFFIC: round(traffic_raw / 1024, 2),
                NAME_DISK_SIZE: disk_size,
                NAME_IP_ADDRESS: self._extract_table_value(html, "Server IP"),
                NAME_TORRENT_CLIENT: self._extract_optional_table_value(html, "Torrent Client"),
                NAME_STATUS: self._extract_table_value(html, "Status"),
            }
        }

    @staticmethod
    def _extract_number(html: str, key: str) -> str:
        match = re.search(rf'\\"{re.escape(key)}\\":(\d+(?:\.\d+)?)', html)
        if not match:
            raise SeedboxDataError(f"Missing dashboard value: {key}")
        return match.group(1)

    @staticmethod
    def _extract_table_value(html: str, label: str) -> str:
        pattern = (
            rf'\\"children\\":\\"{re.escape(label)}\\"'
            rf'.{{0,1400}}?\\"children\\":\\"([^\\"]+)\\"'
        )
        match = re.search(pattern, html)
        if not match:
            raise SeedboxDataError(f"Missing dashboard field: {label}")
        return match.group(1)

    @classmethod
    def _extract_optional_table_value(cls, html: str, label: str) -> str | None:
        try:
            return cls._extract_table_value(html, label)
        except SeedboxDataError:
            return None


seedbox_client = SeedboxClient

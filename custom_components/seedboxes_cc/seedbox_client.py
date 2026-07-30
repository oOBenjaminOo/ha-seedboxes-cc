"""Client for the Seedboxes.cc dashboard."""

from __future__ import annotations

import asyncio
from html import unescape
from html.parser import HTMLParser
import re
from typing import Any
from urllib.parse import urljoin, urlparse

import aiohttp
from yarl import URL

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

BASE_URL = "https://www.seedboxes.cc"
LOGIN_URL = f"{BASE_URL}/api/auth/login"
GATEKEEPER_HOST = "gatekeeper.seedboxes.cc"
USER_AGENT = "HomeAssistant Seedboxes.cc Integration/2.0"


class SeedboxAuthenticationError(Exception):
    """Raised when Seedboxes.cc authentication fails."""


class SeedboxDataError(Exception):
    """Raised when dashboard data cannot be parsed."""


class _LoginFormParser(HTMLParser):
    """Extract the Keycloak login form and its default fields."""

    def __init__(self) -> None:
        super().__init__()
        self.action: str | None = None
        self.fields: dict[str, str] = {}
        self._inside_form = False
        self._candidate_action: str | None = None
        self._candidate_fields: dict[str, str] = {}
        self._candidate_has_password = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "form" and not self._inside_form:
            self._inside_form = True
            self._candidate_action = attributes.get("action")
            self._candidate_fields = {}
            self._candidate_has_password = False
            return

        if tag != "input" or not self._inside_form:
            return

        name = attributes.get("name")
        if not name:
            return

        input_type = (attributes.get("type") or "text").lower()
        if input_type == "password":
            self._candidate_has_password = True

        self._candidate_fields[name] = attributes.get("value") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag != "form" or not self._inside_form:
            return

        if self.action is None and self._candidate_has_password:
            self.action = self._candidate_action
            self.fields = self._candidate_fields

        self._inside_form = False


class SeedboxClient:
    """Authenticate and retrieve information from the Seedboxes.cc dashboard."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        seedbox_id: str,
        username: str,
        password: str,
    ) -> None:
        self._session = session
        self._seedbox_id = str(seedbox_id)
        self._username = username.strip()
        self._password = password
        self._session_cookie: str | None = None
        self._login_lock = asyncio.Lock()

    async def async_validate_credentials(self) -> None:
        """Authenticate and verify access to the configured seedbox."""
        await self._async_login(force=True)
        await self.async_get_data()

    async def async_get_data(self) -> dict[str, Any]:
        """Fetch and parse the Seedboxes.cc dashboard."""
        if self._session_cookie is None:
            await self._async_login()

        try:
            html = await self._async_fetch_dashboard()
        except SeedboxAuthenticationError:
            await self._async_login(force=True)
            html = await self._async_fetch_dashboard()

        if f'"seedboxId":"{self._seedbox_id}"' not in html:
            raise SeedboxAuthenticationError(
                "The requested seedbox is not available for this account"
            )

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
                NAME_TORRENT_CLIENT: self._extract_optional_table_value(
                    html, "Torrent Client"
                ),
                NAME_STATUS: self._extract_table_value(html, "Status"),
            }
        }

    async def _async_login(self, force: bool = False) -> None:
        """Create an authenticated Seedboxes.cc dashboard session."""
        async with self._login_lock:
            if self._session_cookie is not None and not force:
                return

            timeout = aiohttp.ClientTimeout(total=45)
            cookie_jar = aiohttp.CookieJar()
            headers = {"User-Agent": USER_AGENT}

            async with aiohttp.ClientSession(
                timeout=timeout,
                cookie_jar=cookie_jar,
                headers=headers,
            ) as login_session:
                async with login_session.get(LOGIN_URL, allow_redirects=True) as response:
                    if response.status != 200:
                        raise SeedboxDataError(
                            f"Login page returned HTTP {response.status}"
                        )
                    login_page = await response.text()
                    login_page_url = str(response.url)

                parser = _LoginFormParser()
                parser.feed(login_page)
                if not parser.action:
                    raise SeedboxDataError(
                        "Unable to find the Seedboxes.cc authentication form"
                    )

                authentication_url = urljoin(login_page_url, unescape(parser.action))
                parsed_url = urlparse(authentication_url)
                if (
                    parsed_url.scheme != "https"
                    or parsed_url.hostname != GATEKEEPER_HOST
                    or "/login-actions/authenticate" not in parsed_url.path
                ):
                    raise SeedboxDataError("Unexpected authentication endpoint")

                form_data = dict(parser.fields)
                form_data["username"] = self._username
                form_data["password"] = self._password
                form_data.setdefault("credentialId", "")

                async with login_session.post(
                    authentication_url,
                    data=form_data,
                    allow_redirects=True,
                ) as response:
                    final_url = str(response.url)
                    response_text = await response.text()
                    if response.status != 200:
                        raise SeedboxAuthenticationError(
                            f"Authentication returned HTTP {response.status}"
                        )

                session_cookie = cookie_jar.filter_cookies(URL(BASE_URL)).get("session_id")
                if session_cookie is None:
                    if "login-actions/authenticate" in final_url or "kc-form-login" in response_text:
                        raise SeedboxAuthenticationError(
                            "Invalid Seedboxes.cc username or password"
                        )
                    raise SeedboxDataError(
                        "Seedboxes.cc did not create an authenticated session"
                    )

                self._session_cookie = session_cookie.value

    async def _async_fetch_dashboard(self) -> str:
        """Fetch the dashboard using the current authenticated session."""
        url = f"{BASE_URL}/dashboard/seedboxes/{self._seedbox_id}"
        headers = {
            "Cookie": f"session_id={self._session_cookie}",
            "User-Agent": USER_AGENT,
        }

        async with self._session.get(url, headers=headers, allow_redirects=False) as response:
            if response.status in (301, 302, 303, 307, 308, 401, 403):
                self._session_cookie = None
                raise SeedboxAuthenticationError("Session is invalid or expired")
            if response.status != 200:
                raise SeedboxDataError(f"Dashboard returned HTTP {response.status}")
            return await response.text()

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

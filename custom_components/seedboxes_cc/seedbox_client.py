"""Client for the Seedboxes.cc dashboard."""

from __future__ import annotations

import asyncio
from html import unescape
from html.parser import HTMLParser
import json
import logging
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
TELEMETRY_URL = f"{BASE_URL}/api/telemetry/stream-all"
GATEKEEPER_HOST = "gatekeeper.seedboxes.cc"
USER_AGENT = "HomeAssistant Seedboxes.cc Integration/2.0"

_LOGGER = logging.getLogger(__name__)


class SeedboxAuthenticationError(Exception):
    """Raised when Seedboxes.cc authentication fails."""


class SeedboxDataError(Exception):
    """Raised when Seedboxes.cc data cannot be retrieved."""


class _LoginFormParser(HTMLParser):
    """Extract a Keycloak authentication form and its input fields."""

    def __init__(self) -> None:
        super().__init__()
        self.action: str | None = None
        self.fields: dict[str, str] = {}
        self.field_types: dict[str, str] = {}
        self.forms: list[tuple[str | None, dict[str, str]]] = []
        self.title: str | None = None
        self._inside_form = False
        self._inside_title = False
        self._title_parts: list[str] = []
        self._candidate_action: str | None = None
        self._candidate_fields: dict[str, str] = {}
        self._candidate_types: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "title":
            self._inside_title = True
            return
        if tag == "form" and not self._inside_form:
            self._inside_form = True
            self._candidate_action = attributes.get("action")
            self._candidate_fields = {}
            self._candidate_types = {}
            return
        if tag != "input" or not self._inside_form:
            return
        name = attributes.get("name")
        if not name:
            return
        self._candidate_fields[name] = attributes.get("value") or ""
        self._candidate_types[name] = (attributes.get("type") or "text").lower()

    def handle_data(self, data: str) -> None:
        if self._inside_title:
            self._title_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._inside_title = False
            title = " ".join("".join(self._title_parts).split())
            self.title = title or None
            return
        if tag != "form" or not self._inside_form:
            return
        action = self._candidate_action or ""
        self.forms.append((self._candidate_action, dict(self._candidate_types)))
        if self.action is None and "/login-actions/" in unescape(action):
            self.action = self._candidate_action
            self.fields = self._candidate_fields
            self.field_types = self._candidate_types
        self._inside_form = False

    @property
    def has_username(self) -> bool:
        """Return whether this step accepts a username."""
        return "username" in self.fields

    @property
    def has_password(self) -> bool:
        """Return whether this step accepts a password."""
        return "password" in self.fields or any(
            field_type == "password" for field_type in self.field_types.values()
        )


class SeedboxClient:
    """Authenticate and retrieve Seedboxes.cc information."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        seedbox_id: str | None,
        username: str,
        password: str,
    ) -> None:
        self._session = session
        self._seedbox_id = str(seedbox_id) if seedbox_id is not None else None
        self._username = username.strip()
        self._password = password
        self._cookie_header: str | None = None
        self._login_lock = asyncio.Lock()

    async def async_validate_credentials(self) -> None:
        """Authenticate and verify access to the configured seedbox."""
        await self._async_login(force=True)
        if self._seedbox_id is not None:
            await self._async_fetch_seedbox(self._seedbox_id)

    async def async_discover_seedboxes(self) -> dict[str, dict[str, Any]]:
        """Discover all seedboxes exposed by the authenticated telemetry stream."""
        await self._async_login(force=True)
        ids = await self._async_read_telemetry_ids()
        if not ids:
            raise SeedboxDataError("No seedbox was discovered for this account")
        result: dict[str, dict[str, Any]] = {}
        for seedbox_id in sorted(ids):
            result[seedbox_id] = await self._async_fetch_seedbox(seedbox_id)
        return result

    async def async_get_data(self) -> dict[str, Any]:
        """Fetch structured seedbox data from the JSON API and telemetry stream."""
        if self._seedbox_id is None:
            raise SeedboxDataError("No seedbox is selected")
        if self._cookie_header is None:
            await self._async_login()
        try:
            details = await self._async_fetch_seedbox(self._seedbox_id)
            telemetry = await self._async_read_telemetry_sample(self._seedbox_id)
        except SeedboxAuthenticationError:
            await self._async_login(force=True)
            details = await self._async_fetch_seedbox(self._seedbox_id)
            telemetry = await self._async_read_telemetry_sample(self._seedbox_id)

        disk_size = float(details.get("disk_space") or 0)
        disk_used = round(float(telemetry.get("disk_used_bytes", 0)) / 1_000_000_000, 2)
        disk_free = round(max(disk_size - disk_used, 0), 2)
        disk_used_pct = round((disk_used / disk_size) * 100, 2) if disk_size else 0
        server = details.get("server") or {}
        product = details.get("product") or {}

        return {
            "data": {
                NAME_DISK_QUOTA_FREE: disk_free,
                NAME_DISK_QUOTA_USED: disk_used,
                NAME_DISK_QUOTA_USED_PCT: disk_used_pct,
                NAME_MONTHLY_TRAFFIC: float(details.get("monthly_traffic") or 0) * 1000,
                NAME_DISK_SIZE: disk_size,
                NAME_IP_ADDRESS: server.get("ip"),
                NAME_TORRENT_CLIENT: details.get("torrent_client"),
                NAME_STATUS: product.get("status") or telemetry.get("status") or "Unknown",
            }
        }

    async def _async_login(self, force: bool = False) -> None:
        """Create an authenticated Seedboxes.cc session through Keycloak."""
        async with self._login_lock:
            if self._cookie_header is not None and not force:
                return

            self._cookie_header = None
            timeout = aiohttp.ClientTimeout(total=60)
            cookie_jar = aiohttp.CookieJar()
            async with aiohttp.ClientSession(
                timeout=timeout,
                cookie_jar=cookie_jar,
                headers={"User-Agent": USER_AGENT},
            ) as login_session:
                async with login_session.get(LOGIN_URL, allow_redirects=True) as response:
                    if response.status != 200:
                        raise SeedboxDataError(
                            f"Login page returned HTTP {response.status}"
                        )
                    page_text = await response.text()
                    page_url = str(response.url)
                    parser = self._parse_and_log_login_page(
                        page_text,
                        page_url,
                        response.status,
                        response.headers.get("Content-Type"),
                        "initial",
                    )

                submitted_username = False
                submitted_password = False

                for _step in range(4):
                    cookie_header = self._build_cookie_header(cookie_jar)
                    if cookie_header and urlparse(page_url).hostname == "www.seedboxes.cc":
                        self._cookie_header = cookie_header
                        return

                    if not parser.action:
                        raise SeedboxDataError(
                            "Unable to find the Keycloak authentication form"
                        )

                    authentication_url = urljoin(page_url, unescape(parser.action))
                    parsed_url = urlparse(authentication_url)
                    if (
                        parsed_url.scheme != "https"
                        or parsed_url.hostname != GATEKEEPER_HOST
                        or "/login-actions/" not in parsed_url.path
                    ):
                        raise SeedboxDataError(
                            "Unexpected Keycloak authentication endpoint"
                        )

                    form_data = dict(parser.fields)
                    if parser.has_username:
                        form_data["username"] = self._username
                        submitted_username = True
                    if parser.has_password:
                        form_data["password"] = self._password
                        submitted_password = True
                    form_data.setdefault("credentialId", "")

                    async with login_session.post(
                        authentication_url,
                        data=form_data,
                        allow_redirects=True,
                    ) as response:
                        page_url = str(response.url)
                        page_text = await response.text()
                        parser = self._parse_and_log_login_page(
                            page_text,
                            page_url,
                            response.status,
                            response.headers.get("Content-Type"),
                            f"authentication-step-{_step + 1}",
                        )
                        if response.status != 200:
                            raise SeedboxAuthenticationError(
                                f"Authentication returned HTTP {response.status}"
                            )

                cookie_header = self._build_cookie_header(cookie_jar)
                if cookie_header:
                    self._cookie_header = cookie_header
                    return

                if submitted_username or submitted_password:
                    raise SeedboxAuthenticationError(
                        "Seedboxes.cc rejected the username or password"
                    )
                raise SeedboxDataError(
                    "Seedboxes.cc did not complete the authentication flow"
                )

    def _parse_and_log_login_page(
        self,
        page_text: str,
        page_url: str,
        status: int,
        content_type: str | None,
        stage: str,
    ) -> _LoginFormParser:
        """Parse and safely log the structure of one authentication page."""
        parser = _LoginFormParser()
        parser.feed(page_text)

        def safe_text(value: str | None) -> str | None:
            if value is None:
                return None
            cleaned = " ".join(value.split())[:200]
            for secret in (self._username, self._password):
                if secret:
                    cleaned = cleaned.replace(secret, "<redacted>")
            return cleaned

        def safe_url(value: str | None) -> str | None:
            if not value:
                return None
            parsed = urlparse(urljoin(page_url, unescape(value)))
            hostname = parsed.hostname or ""
            if parsed.port:
                hostname = f"{hostname}:{parsed.port}"
            return parsed._replace(
                netloc=hostname,
                params="",
                query="",
                fragment="",
            ).geturl()

        forms = [
            {
                "action": safe_url(action),
                "fields": sorted(
                    (
                        safe_text(name),
                        safe_text(field_type),
                    )
                    for name, field_type in field_types.items()
                ),
            }
            for action, field_types in parser.forms
        ]
        lower_page = page_text.lower()
        keywords = {
            "keycloak": "keycloak" in lower_page,
            "cloudflare": "cloudflare" in lower_page,
            "javascript": "javascript" in lower_page,
        }
        _LOGGER.warning(
            "Seedboxes.cc login diagnostic: stage=%s status=%s "
            "final_url=%s content_type=%s title=%s forms=%s keywords=%s",
            stage,
            status,
            safe_url(page_url),
            safe_text(content_type),
            safe_text(parser.title),
            forms,
            keywords,
        )
        return parser

    @staticmethod
    def _build_cookie_header(cookie_jar: aiohttp.CookieJar) -> str | None:
        """Build a complete Cookie header for the Seedboxes.cc API host."""
        cookies = cookie_jar.filter_cookies(URL(BASE_URL))
        if not cookies:
            return None
        return "; ".join(
            f"{name}={morsel.value}" for name, morsel in cookies.items()
        )

    def _headers(self) -> dict[str, str]:
        if self._cookie_header is None:
            raise SeedboxAuthenticationError("No authenticated session is available")
        return {
            "Cookie": self._cookie_header,
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }

    def _invalidate_session(self) -> None:
        """Forget the current authenticated cookie set."""
        self._cookie_header = None

    async def _async_fetch_seedbox(self, seedbox_id: str) -> dict[str, Any]:
        """Fetch one seedbox from the JSON endpoint."""
        url = f"{BASE_URL}/api/seedbox/{seedbox_id}"
        async with self._session.get(
            url, headers=self._headers(), allow_redirects=False
        ) as response:
            if response.status in (301, 302, 303, 307, 308, 401, 403):
                self._invalidate_session()
                raise SeedboxAuthenticationError("Session is invalid or expired")
            if response.status == 404:
                raise SeedboxAuthenticationError(
                    "Seedbox is not available for this account"
                )
            if response.status != 200:
                raise SeedboxDataError(
                    f"Seedbox API returned HTTP {response.status}"
                )
            payload = await response.json(content_type=None)
        if not payload.get("success") or not isinstance(payload.get("data"), dict):
            raise SeedboxDataError("Seedbox API returned an invalid response")
        return payload["data"]

    async def _async_read_telemetry_ids(self) -> set[str]:
        """Read seedbox identifiers from the SSE stream."""
        ids: set[str] = set()
        expected: int | None = None
        async with asyncio.timeout(20):
            async with self._session.get(
                TELEMETRY_URL,
                headers={**self._headers(), "Accept": "text/event-stream"},
                allow_redirects=False,
            ) as response:
                self._check_stream_response(response)
                async for raw_line in response.content:
                    event = self._parse_sse_line(raw_line)
                    if event is None:
                        continue
                    if event.get("type") == "connected":
                        expected = int(event.get("seedboxCount") or 0)
                    if event.get("seedboxId") is not None:
                        ids.add(str(event["seedboxId"]))
                    if expected is not None and expected > 0 and len(ids) >= expected:
                        break
        return ids

    async def _async_read_telemetry_sample(self, seedbox_id: str) -> dict[str, Any]:
        """Read a current disk-space sample for one seedbox."""
        result: dict[str, Any] = {}
        async with asyncio.timeout(20):
            async with self._session.get(
                TELEMETRY_URL,
                headers={**self._headers(), "Accept": "text/event-stream"},
                allow_redirects=False,
            ) as response:
                self._check_stream_response(response)
                async for raw_line in response.content:
                    event = self._parse_sse_line(raw_line)
                    if event is None or str(event.get("seedboxId", "")) != seedbox_id:
                        continue
                    result["status"] = event.get("status")
                    data = event.get("data") or {}
                    if data.get("type") == "diskspace" and data.get("metric") == "used":
                        result["disk_used_bytes"] = float(data.get("value") or 0)
                        break
        if "disk_used_bytes" not in result:
            raise SeedboxDataError("No disk-space telemetry was received")
        return result

    def _check_stream_response(self, response: aiohttp.ClientResponse) -> None:
        if response.status in (301, 302, 303, 307, 308, 401, 403):
            self._invalidate_session()
            raise SeedboxAuthenticationError("Session is invalid or expired")
        if response.status != 200:
            raise SeedboxDataError(
                f"Telemetry stream returned HTTP {response.status}"
            )

    @staticmethod
    def _parse_sse_line(raw_line: bytes) -> dict[str, Any] | None:
        line = raw_line.decode(errors="replace").strip()
        if not line.startswith("data:"):
            return None
        try:
            payload = json.loads(line[5:].strip())
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None


seedbox_client = SeedboxClient

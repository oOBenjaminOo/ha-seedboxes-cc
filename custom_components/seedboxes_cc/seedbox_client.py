"""Client for the Seedboxes.cc dashboard."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
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
)

BASE_URL = "https://www.seedboxes.cc"
LOGIN_URL = f"{BASE_URL}/api/auth/login"
TELEMETRY_URL = f"{BASE_URL}/api/telemetry/stream-all"
GATEKEEPER_HOST = "gatekeeper.seedboxes.cc"
USER_AGENT = "HomeAssistant Seedboxes.cc Integration/2.0"
ALLOWED_LOGIN_HOSTS = {"www.seedboxes.cc", GATEKEEPER_HOST}
AUTH_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
SESSION_EXPIRED_STATUSES = AUTH_REDIRECT_STATUSES | {401, 403}
MAX_LOGIN_REDIRECTS = 8

_LOGGER = logging.getLogger(__name__)


class SeedboxAuthenticationError(Exception):
    """Raised when Seedboxes.cc authentication fails."""


class SeedboxSessionExpiredError(SeedboxAuthenticationError):
    """Raised when an authenticated Seedboxes.cc session has expired."""


class SeedboxDataError(Exception):
    """Raised when Seedboxes.cc data cannot be retrieved."""


class SeedboxDiscoveryError(SeedboxDataError):
    """Raised when a valid session does not expose a seedbox ID."""


class SeedboxBrowserVerificationRequired(SeedboxDataError):
    """Raised when browser verification prevents automatic authentication."""


def validate_seedbox_id(seedbox_id: str) -> str:
    """Return a safe numeric seedbox identifier."""
    normalized = str(seedbox_id).strip()
    if not normalized.isascii() or not normalized.isdigit() or len(normalized) > 20:
        raise ValueError("Seedbox ID must contain only digits")
    return normalized


def validate_session_cookie(session_cookie: str) -> str:
    """Return a raw session_id value that is safe for an HTTP Cookie header."""
    normalized = str(session_cookie).strip()
    if (
        not normalized
        or len(normalized) > 4096
        or normalized.lower() in {"deleted", "expired", "revoked"}
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
        or any(separator in normalized for separator in ";,")
    ):
        raise ValueError("Invalid session cookie value")
    return normalized


def _safe_login_url(url: str) -> str:
    """Validate an authentication URL before sending a request."""
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError as err:
        raise SeedboxDataError("Unexpected authentication endpoint") from err
    if (
        parsed.scheme != "https"
        or parsed.hostname not in ALLOWED_LOGIN_HOSTS
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise SeedboxDataError("Unexpected authentication endpoint")
    return url


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
        self._seedbox_id = (
            validate_seedbox_id(seedbox_id) if seedbox_id is not None else None
        )
        self._username = username.strip()
        self._password = password
        self._session_cookie: str | None = None
        self._login_lock = asyncio.Lock()

    @property
    def session_cookie(self) -> str | None:
        """Return the current raw session_id value."""
        return self._session_cookie

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
        if self._session_cookie is None:
            await self._async_login()
        try:
            details = await self._async_fetch_seedbox(self._seedbox_id)
            telemetry = await self._async_read_telemetry_sample(self._seedbox_id)
        except SeedboxSessionExpiredError:
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
                NAME_STATUS: product.get("status")
                or telemetry.get("status")
                or "Unknown",
            }
        }

    async def _async_login(self, force: bool = False) -> None:
        """Create an authenticated Seedboxes.cc session through Keycloak."""
        async with self._login_lock:
            if self._session_cookie is not None and not force:
                return

            self._session_cookie = None
            timeout = aiohttp.ClientTimeout(total=60)
            cookie_jar = aiohttp.CookieJar()
            async with aiohttp.ClientSession(
                timeout=timeout,
                cookie_jar=cookie_jar,
                headers={"User-Agent": USER_AGENT},
            ) as login_session:
                (
                    status,
                    page_url,
                    page_text,
                    content_type,
                ) = await self._async_login_request(
                    login_session,
                    "GET",
                    LOGIN_URL,
                )
                parser = self._parse_and_log_login_page(
                    page_text,
                    page_url,
                    status,
                    content_type,
                    "initial",
                )
                if status != 200:
                    lower_page = page_text.lower()
                    if any(
                        marker in lower_page
                        for marker in ("turnstile", "cloudflare", "cf-chl-")
                    ):
                        raise SeedboxBrowserVerificationRequired(
                            "Seedboxes.cc requires browser verification; "
                            "use session cookie authentication"
                        )
                    raise SeedboxDataError(f"Login page returned HTTP {status}")

                submitted_username = False
                submitted_password = False

                for _step in range(4):
                    session_cookie = self._read_session_cookie(cookie_jar)
                    if (
                        session_cookie
                        and urlparse(page_url).hostname == "www.seedboxes.cc"
                    ):
                        self._session_cookie = session_cookie
                        return

                    if not parser.action:
                        lower_page = page_text.lower()
                        if "turnstile" in lower_page or (
                            urlparse(page_url).hostname == GATEKEEPER_HOST
                            and "keycloak" in lower_page
                        ):
                            raise SeedboxBrowserVerificationRequired(
                                "Seedboxes.cc requires browser verification; "
                                "use session cookie authentication"
                            )
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
                        if submitted_username and not parser.has_password:
                            raise SeedboxAuthenticationError(
                                "Seedboxes.cc rejected the username or password"
                            )
                        form_data["username"] = self._username
                        submitted_username = True
                    if parser.has_password:
                        if submitted_password:
                            raise SeedboxAuthenticationError(
                                "Seedboxes.cc rejected the username or password"
                            )
                        form_data["password"] = self._password
                        submitted_password = True
                    form_data.setdefault("credentialId", "")

                    (
                        status,
                        page_url,
                        page_text,
                        content_type,
                    ) = await self._async_login_request(
                        login_session,
                        "POST",
                        authentication_url,
                        form_data,
                    )
                    parser = self._parse_and_log_login_page(
                        page_text,
                        page_url,
                        status,
                        content_type,
                        f"authentication-step-{_step + 1}",
                    )
                    if status != 200:
                        raise SeedboxAuthenticationError(
                            f"Authentication returned HTTP {status}"
                        )

                session_cookie = self._read_session_cookie(cookie_jar)
                if session_cookie:
                    self._session_cookie = session_cookie
                    return

                if submitted_username or submitted_password:
                    raise SeedboxAuthenticationError(
                        "Seedboxes.cc rejected the username or password"
                    )
                raise SeedboxDataError(
                    "Seedboxes.cc did not complete the authentication flow"
                )

    async def _async_login_request(
        self,
        session: aiohttp.ClientSession,
        method: str,
        url: str,
        form_data: dict[str, str] | None = None,
    ) -> tuple[int, str, str, str | None]:
        """Request one login page while following only trusted redirects."""
        current_method = method
        current_url = _safe_login_url(url)
        current_data = form_data

        for redirect_count in range(MAX_LOGIN_REDIRECTS + 1):
            async with session.request(
                current_method,
                current_url,
                data=current_data,
                allow_redirects=False,
            ) as response:
                status = response.status
                response_url = str(response.url)
                content_type = response.headers.get("Content-Type")
                if status not in AUTH_REDIRECT_STATUSES:
                    return (
                        status,
                        response_url,
                        await response.text(),
                        content_type,
                    )

                location = response.headers.get("Location")
                if not location:
                    raise SeedboxDataError(
                        "Authentication redirect did not provide a destination"
                    )
                if redirect_count >= MAX_LOGIN_REDIRECTS:
                    raise SeedboxDataError("Too many authentication redirects")

                destination = _safe_login_url(urljoin(response_url, unescape(location)))
                if current_method == "POST" and status in (307, 308):
                    raise SeedboxDataError(
                        "Unsafe authentication redirect was rejected"
                    )
                if status == 303 or (current_method == "POST" and status in (301, 302)):
                    current_method = "GET"
                    current_data = None
                current_url = destination

        raise SeedboxDataError("Too many authentication redirects")

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
            cleaned = value
            for secret in (self._username, self._password, self._seedbox_id):
                if secret:
                    cleaned = re.sub(
                        re.escape(secret),
                        "<redacted>",
                        cleaned,
                        flags=re.IGNORECASE,
                    )
            cleaned = re.sub(
                r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
                "<redacted-email>",
                cleaned,
                flags=re.IGNORECASE,
            )
            cleaned = re.sub(
                r"\b[A-Za-z0-9_-]{24,}\b",
                "<redacted-token>",
                cleaned,
            )
            return " ".join(cleaned.split())[:200]

        def safe_url(value: str | None) -> str | None:
            if not value:
                return None
            try:
                parsed = urlparse(urljoin(page_url, unescape(value)))
                hostname = parsed.hostname or ""
                if parsed.port:
                    hostname = f"{hostname}:{parsed.port}"
            except ValueError:
                return "<invalid-url>"
            return parsed._replace(
                netloc=hostname,
                path=safe_text(parsed.path) or "",
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
    def _read_session_cookie(cookie_jar: aiohttp.CookieJar) -> str | None:
        """Return the authenticated Seedboxes.cc session cookie value."""
        cookies = cookie_jar.filter_cookies(URL(BASE_URL))
        session_cookie = cookies.get("session_id")
        if session_cookie is None or not session_cookie.value:
            return None
        try:
            return validate_session_cookie(session_cookie.value)
        except ValueError:
            return None

    def _headers(self) -> dict[str, str]:
        if self._session_cookie is None:
            raise SeedboxAuthenticationError("No authenticated session is available")
        return {
            "Cookie": f"session_id={self._session_cookie}",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }

    def _invalidate_session(self) -> None:
        """Forget the current authenticated cookie set."""
        self._session_cookie = None

    async def _async_fetch_seedbox(self, seedbox_id: str) -> dict[str, Any]:
        """Fetch one seedbox from the JSON endpoint."""
        url = f"{BASE_URL}/api/seedbox/{seedbox_id}"
        async with self._session.get(
            url, headers=self._headers(), allow_redirects=False
        ) as response:
            if response.status in SESSION_EXPIRED_STATUSES:
                self._invalidate_session()
                raise SeedboxSessionExpiredError("Session is invalid or expired")
            if response.status == 404:
                raise SeedboxAuthenticationError(
                    "Seedbox is not available for this account"
                )
            if response.status != 200:
                raise SeedboxDataError(f"Seedbox API returned HTTP {response.status}")
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
                        try:
                            ids.add(validate_seedbox_id(str(event["seedboxId"])))
                        except ValueError:
                            continue
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
        if response.status in SESSION_EXPIRED_STATUSES:
            self._invalidate_session()
            raise SeedboxSessionExpiredError("Session is invalid or expired")
        if response.status != 200:
            raise SeedboxDataError(f"Telemetry stream returned HTTP {response.status}")

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


class SessionCookieSeedboxClient:
    """Retrieve seedbox information with a browser session cookie."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        seedbox_id: str | None,
        session_cookie: str,
    ) -> None:
        self._session = session
        self._seedbox_id = (
            validate_seedbox_id(seedbox_id) if seedbox_id is not None else None
        )
        self._session_cookie = validate_session_cookie(session_cookie)

    @property
    def session_cookie(self) -> str:
        """Return the current raw session_id value."""
        return self._session_cookie

    async def async_validate_credentials(self) -> None:
        """Validate the browser session and seedbox access."""
        if self._seedbox_id is None:
            await self.async_discover_seedboxes()
        else:
            await self.async_get_data()

    async def async_discover_seedboxes(self) -> dict[str, dict[str, Any]]:
        """Discover seedbox IDs available to this browser session."""
        try:
            ids = await self._async_read_telemetry_ids()
        except SeedboxSessionExpiredError:
            raise
        except (SeedboxDataError, TimeoutError):
            ids = set()

        if not ids:
            ids = await self._async_read_dashboard_ids()
        if not ids:
            raise SeedboxDiscoveryError(
                "No seedbox ID could be discovered from this session"
            )
        return {seedbox_id: {} for seedbox_id in sorted(ids)}

    async def async_get_data(self) -> dict[str, Any]:
        """Fetch and parse the authenticated dashboard page."""
        if self._seedbox_id is None:
            raise SeedboxDataError("No seedbox is selected")
        url = f"{BASE_URL}/dashboard/seedboxes/{self._seedbox_id}"

        async with self._session.get(
            url, headers=self._headers(), allow_redirects=False
        ) as response:
            if response.status in SESSION_EXPIRED_STATUSES:
                raise SeedboxSessionExpiredError("Session cookie is invalid or expired")
            if response.status == 404:
                raise SeedboxAuthenticationError(
                    "The requested seedbox is not available in this session"
                )
            if response.status != 200:
                raise SeedboxDataError(f"Dashboard returned HTTP {response.status}")
            replacement_cookie = self._response_session_cookie(response)
            html = await response.text()

        if f'\\"seedboxId\\":\\"{self._seedbox_id}\\"' not in html:
            if self._looks_like_authentication_page(html):
                raise SeedboxSessionExpiredError("Session cookie is invalid or expired")
            raise SeedboxDataError(
                "Dashboard response did not contain the selected seedbox"
            )

        disk_size = float(self._extract_number(html, "diskSpaceLimit"))
        traffic_raw = float(self._extract_number(html, "currentMonthTraffic"))
        metrics = re.findall(
            r'\\"diskspace\\":(\d+),\\"traffic\\":(\d+)',
            html,
        )
        if not metrics:
            raise SeedboxDataError("No telemetry metrics found in dashboard page")

        disk_used_mb = float(metrics[-1][0])
        disk_used_gb = round(disk_used_mb / 1000, 2)
        disk_free_gb = round(max(disk_size - disk_used_gb, 0), 2)
        disk_used_pct = round((disk_used_gb / disk_size) * 100, 2) if disk_size else 0

        result = {
            "data": {
                NAME_DISK_QUOTA_FREE: disk_free_gb,
                NAME_DISK_QUOTA_USED: disk_used_gb,
                NAME_DISK_QUOTA_USED_PCT: disk_used_pct,
                NAME_MONTHLY_TRAFFIC: round(traffic_raw / 1024, 2),
                NAME_DISK_SIZE: disk_size,
                NAME_IP_ADDRESS: self._extract_table_value(html, "Server IP"),
                NAME_STATUS: self._extract_table_value(html, "Status"),
            }
        }
        if replacement_cookie is not None:
            self._session_cookie = replacement_cookie
        return result

    async def _async_read_telemetry_ids(self) -> set[str]:
        """Read seedbox identifiers from the authenticated SSE stream."""
        ids: set[str] = set()
        expected: int | None = None
        replacement_cookie: str | None = None
        async with asyncio.timeout(20):
            async with self._session.get(
                TELEMETRY_URL,
                headers=self._headers("text/event-stream"),
                allow_redirects=False,
            ) as response:
                if response.status in SESSION_EXPIRED_STATUSES:
                    raise SeedboxSessionExpiredError(
                        "Session cookie is invalid or expired"
                    )
                if response.status != 200:
                    raise SeedboxDataError(
                        f"Telemetry stream returned HTTP {response.status}"
                    )
                replacement_cookie = self._response_session_cookie(response)
                async for raw_line in response.content:
                    event = SeedboxClient._parse_sse_line(raw_line)
                    if event is None:
                        continue
                    if event.get("type") == "connected":
                        expected = int(event.get("seedboxCount") or 0)
                        if expected == 0:
                            break
                    if event.get("seedboxId") is not None:
                        candidate = str(event["seedboxId"])
                        try:
                            ids.add(validate_seedbox_id(candidate))
                        except ValueError:
                            continue
                    if expected is not None and len(ids) >= expected:
                        break
        if ids and replacement_cookie is not None:
            self._session_cookie = replacement_cookie
        return ids

    async def _async_read_dashboard_ids(self) -> set[str]:
        """Find seedbox identifiers in the authenticated dashboard HTML."""
        async with self._session.get(
            f"{BASE_URL}/dashboard",
            headers=self._headers(),
            allow_redirects=False,
        ) as response:
            if response.status in SESSION_EXPIRED_STATUSES:
                raise SeedboxSessionExpiredError("Session cookie is invalid or expired")
            if response.status != 200:
                raise SeedboxDataError(f"Dashboard returned HTTP {response.status}")
            replacement_cookie = self._response_session_cookie(response)
            html = await response.text()

        normalized_html = html.replace('\\"', '"')
        candidates = set(re.findall(r'"seedboxId"\s*:\s*"(\d+)"', normalized_html))
        candidates.update(re.findall(r"/dashboard/seedboxes/(\d+)", normalized_html))
        ids: set[str] = set()
        for candidate in candidates:
            try:
                ids.add(validate_seedbox_id(candidate))
            except ValueError:
                continue
        if ids and replacement_cookie is not None:
            self._session_cookie = replacement_cookie
        return ids

    def _headers(self, accept: str = "text/html") -> dict[str, str]:
        """Return headers for an authenticated browser-session request."""
        return {
            "Cookie": f"session_id={self._session_cookie}",
            "User-Agent": USER_AGENT,
            "Accept": accept,
        }

    @staticmethod
    def _response_session_cookie(
        response: aiohttp.ClientResponse,
    ) -> str | None:
        """Read a rotated session_id only from an authenticated site response."""
        if urlparse(str(response.url)).hostname != "www.seedboxes.cc":
            return None
        session_cookie = response.cookies.get("session_id")
        if session_cookie is None:
            return None
        max_age = session_cookie["max-age"]
        if max_age:
            try:
                if int(max_age) <= 0:
                    return None
            except ValueError:
                return None
        expires = session_cookie["expires"]
        if expires:
            try:
                expires_at = parsedate_to_datetime(expires)
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                if expires_at <= datetime.now(timezone.utc):
                    return None
            except (TypeError, ValueError, OverflowError):
                return None
        try:
            return validate_session_cookie(session_cookie.value)
        except ValueError:
            return None

    @staticmethod
    def _looks_like_authentication_page(html: str) -> bool:
        """Return whether a successful HTTP page is actually a login screen."""
        lower_html = html.lower()
        has_login_form = "<form" in lower_html and (
            "/login-actions/" in lower_html
            or 'name="password"' in lower_html
            or "name='password'" in lower_html
        )
        has_browser_challenge = any(
            marker in lower_html for marker in ("cf-turnstile", "cf-chl-")
        )
        return has_login_form or has_browser_challenge

    @staticmethod
    def _extract_number(html: str, key: str) -> str:
        match = re.search(
            rf'\\"{re.escape(key)}\\":(\d+(?:\.\d+)?)',
            html,
        )
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


class HybridSeedboxClient:
    """Use a saved cookie first and renew it once with account credentials."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        seedbox_id: str,
        username: str | None,
        password: str | None,
        session_cookie: str | None,
    ) -> None:
        self._session = session
        self._seedbox_id = validate_seedbox_id(seedbox_id)
        self._username = username.strip() if username else None
        self._password = password if password else None
        self._cookie_client = (
            SessionCookieSeedboxClient(
                session,
                self._seedbox_id,
                session_cookie,
            )
            if session_cookie
            else None
        )
        self._credential_client = (
            SeedboxClient(
                session,
                self._seedbox_id,
                self._username,
                self._password,
            )
            if self._username and self._password
            else None
        )
        self._renewal_lock = asyncio.Lock()

    @property
    def session_cookie(self) -> str | None:
        """Return the most recent raw session_id value."""
        if self._cookie_client is not None:
            return self._cookie_client.session_cookie
        if self._credential_client is not None:
            return self._credential_client.session_cookie
        return None

    async def async_validate_credentials(self) -> None:
        """Validate at least one configured authentication method."""
        await self.async_get_data()

    async def async_get_data(self) -> dict[str, Any]:
        """Fetch data and renew only a genuinely expired browser session."""
        if self._cookie_client is None:
            if self._credential_client is None:
                raise SeedboxAuthenticationError(
                    "Account credentials or a session cookie are required"
                )
            async with self._renewal_lock:
                if self._cookie_client is not None:
                    return await self._cookie_client.async_get_data()
                return await self._async_get_with_credentials()

        expired_cookie = self._cookie_client.session_cookie
        try:
            return await self._cookie_client.async_get_data()
        except SeedboxSessionExpiredError:
            if self._credential_client is None:
                raise

        async with self._renewal_lock:
            if (
                self._cookie_client is not None
                and self._cookie_client.session_cookie != expired_cookie
            ):
                return await self._cookie_client.async_get_data()
            return await self._async_get_with_credentials()

    async def _async_get_with_credentials(self) -> dict[str, Any]:
        """Perform one credential-backed update and adopt its new cookie."""
        if self._credential_client is None:
            raise SeedboxAuthenticationError(
                "Account credentials are required to renew the session"
            )
        await self._credential_client.async_get_data()
        session_cookie = self._credential_client.session_cookie
        if session_cookie is None:
            raise SeedboxAuthenticationError(
                "Seedboxes.cc did not provide an authenticated session"
            )
        self._cookie_client = SessionCookieSeedboxClient(
            self._session,
            self._seedbox_id,
            session_cookie,
        )
        return await self._cookie_client.async_get_data()


seedbox_client = SeedboxClient

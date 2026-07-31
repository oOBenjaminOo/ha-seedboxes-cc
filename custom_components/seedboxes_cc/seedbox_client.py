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
    NAME_TORRENT_CLIENT,
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
                NAME_TORRENT_CLIENT: details.get("torrent_client"),
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
        """Request one login page while following only trusted redirects.""ÛÏy¶‰žËkºwµçA¥˜‘…Ñ„¹•Ð ‰ÑåÁ”ˆ¤€ôô€‰‘¥Í­ÍÁ…”ˆ…¹‘…Ñ„¹•Ð ‰µ•ÑÉ¥Œˆ¤€ôô€‰ÕÍ•ˆè(€€€€€€€€€€€€€€€€€€€€€€€É•ÍÕ±Ñl‰‘¥Í­}ÕÍ•‘}‰åÑ•Ì‰t€ô™±½…Ð¡‘…Ñ„¹•Ð ‰Ù…±Õ”ˆ¤½È€À¤(€€€€€€€€€€€€€€€€€€€€€€€‰É•…¬(€€€€€€€¥˜€‰‘¥Í­}ÕÍ•‘}‰åÑ•Ìˆ¹½Ð¥¸É•ÍÕ±Ðè(€€€€€€€€€€€É…¥Í”M••‘‰½á…Ñ…ÉÉ½È ‰9¼‘¥Í¬µÍÁ…”Ñ•±•µ•ÑÉäÝ…ÌÉ••¥Ù•ˆ¤(€€€€€€€É•ÑÕÉ¸É•ÍÕ±Ð((€€€‘•˜}¡•­}ÍÑÉ•…µ}É•ÍÁ½¹Í”¡Í•±˜°É•ÍÁ½¹Í”è…¥½¡ÑÑÀ¹±¥•¹ÑI•ÍÁ½¹Í”¤€´ø9½¹”è(€€€€€€€¥˜É•ÍÁ½¹Í”¹ÍÑ…ÑÕÌ¥¸MMM%=9}aA%I}MQQUMLè(€€€€€€€€€€€Í•±˜¹}¥¹Ù…±¥‘…Ñ•}Í•ÍÍ¥½¸ ¤(€€€€€€€€€€€É…¥Í”M••‘‰½áM•ÍÍ¥½¹áÁ¥É•‘ÉÉ½È ‰M•ÍÍ¥½¸¥Ì¥¹Ù…±¥½È•áÁ¥É•ˆ¤(€€€€€€€¥˜É•ÍÁ½¹Í”¹ÍÑ…ÑÕÌ€„ô€ÈÀÀè(€€€€€€€€€€€É…¥Í”M••‘‰½á…Ñ…ÉÉ½È¡˜‰Q•±•µ•ÑÉäÍÑÉ•…´É•ÑÕÉ¹•!QQ@íÉ•ÍÁ½¹Í”¹ÍÑ…ÑÕÍôˆ¤((€€€ÍÑ…Ñ¥µ•Ñ¡½(€€€‘•˜}Á…ÉÍ•}ÍÍ•}±¥¹”¡É…Ý}±¥¹”è‰åÑ•Ì¤€´ø‘¥ÑmÍÑÈ°¹åtð9½¹”è(€€€€€€€±¥¹”€ôÉ…Ý}±¥¹”¹‘•½‘”¡•ÉÉ½ÉÌô‰É•Á±…”ˆ¤¹ÍÑÉ¥À ¤(€€€€€€€¥˜¹½Ð±¥¹”¹ÍÑ…ÉÑÍÝ¥Ñ  ‰‘…Ñ„èˆ¤è(€€€€€€€€€€€É•ÑÕÉ¸9½¹”(€€€€€€€ÑÉäè(€€€€€€€€€€€Á…å±½…€ô©Í½¸¹±½…‘Ì¡±¥¹•lÔét¹ÍÑÉ¥À ¤¤(€€€€€€€•á•ÁÐ©Í½¸¹)M=9•½‘•ÉÉ½Èè(€€€€€€€€€€€É•ÑÕÉ¸9½¹”(€€€€€€€É•ÑÕÉ¸Á…å±½…¥˜¥Í¥¹ÍÑ…¹”¡Á…å±½…°‘¥Ð¤•±Í”9½¹”(()±…ÍÌM•ÍÍ¥½¹½½­¥•M••‘‰½á±¥•¹Ðè(€€€€ˆˆ‰I•ÑÉ¥•Ù”Í••‘‰½à¥¹™½Éµ…Ñ¥½¸Ý¥Ñ „‰É½ÝÍ•ÈÍ•ÍÍ¥½¸½½­¥”¸ˆˆˆ((€€€‘•˜}}¥¹¥Ñ}| (€€€€€€€Í•±˜°(€€€€€€€Í•ÍÍ¥½¸è…¥½¡ÑÑÀ¹±¥•¹ÑM•ÍÍ¥½¸°(€€€€€€€Í••‘‰½á}¥èÍÑÈð9½¹”°(€€€€€€€Í•ÍÍ¥½¹}½½­¥”èÍÑÈ°(€€€€¤€´ø9½¹”è(€€€€€€€Í•±˜¹}Í•ÍÍ¥½¸€ôÍ•ÍÍ¥½¸(€€€€€€€Í•±˜¹}Í••‘‰½á}¥€ô€ (€€€€€€€€€€€Ù…±¥‘…Ñ•}Í••‘‰½á}¥¡Í••‘‰½á}¥¤¥˜Í••‘‰½á}¥¥Ì¹½Ð9½¹”•±Í”9½¹”(€€€€€€€€¤(€€€€€€€Í•±˜¹}Í•ÍÍ¥½¹}½½­¥”€ôÙ…±¥‘…Ñ•}Í•ÍÍ¥½¹}½½­¥”¡Í•ÍÍ¥½¹}½½­¥”¤((€€€ÁÉ½Á•ÉÑä(€€€‘•˜Í•ÍÍ¥½¹}½½­¥”¡Í•±˜¤€´øÍÑÈè(€€€€€€€€ˆˆ‰I•ÑÕÉ¸Ñ¡”ÕÉÉ•¹ÐÉ…ÜÍ•ÍÍ¥½¹}¥Ù…±Õ”¸ˆˆˆ(€€€€€€€É•ÑÕÉ¸Í•±˜¹}Í•ÍÍ¥½¹}½½­¥”((€€€…Íå¹Œ‘•˜…Íå¹}Ù…±¥‘…Ñ•}É•‘•¹Ñ¥…±Ì¡Í•±˜¤€´ø9½¹”è(€€€€€€€€ˆˆ‰Y…±¥‘…Ñ”Ñ¡”‰É½ÝÍ•ÈÍ•ÍÍ¥½¸…¹Í••‘‰½à…•ÍÌ¸ˆˆˆ(€€€€€€€¥˜Í•±˜¹}Í••‘‰½á}¥¥Ì9½¹”è(€€€€€€€€€€€…Ý…¥ÐÍ•±˜¹…Íå¹}‘¥Í½Ù•É}Í••‘‰½á•Ì ¤(€€€€€€€•±Í”è(€€€€€€€€€€€…Ý…¥ÐÍ•±˜¹…Íå¹}•Ñ}‘…Ñ„ ¤((€€€…Íå¹Œ‘•˜…Íå¹}‘¥Í½Ù•É}Í••‘‰½á•Ì¡Í•±˜¤€´ø‘¥ÑmÍÑÈ°‘¥ÑmÍÑÈ°¹åutè(€€€€€€€€ˆˆ‰¥Í½Ù•ÈÍ••‘‰½à%Ì…Ù…¥±…‰±”Ñ¼Ñ¡¥Ì‰É½ÝÍ•ÈÍ•ÍÍ¥½¸¸ˆˆˆ(€€€€€€€ÑÉäè(€€€€€€€€€€€¥‘Ì€ô…Ý…¥ÐÍ•±˜¹}…Íå¹}É•…‘}Ñ•±•µ•ÑÉå}¥‘Ì ¤(€€€€€€€•á•ÁÐM••‘‰½áM•ÍÍ¥½¹áÁ¥É•‘ÉÉ½Èè(€€€€€€€€€€€É…¥Í”(€€€€€€€•á•ÁÐ€¡M••‘‰½á…Ñ…ÉÉ½È°Q¥µ•½ÕÑÉÉ½È¤è(€€€€€€€€€€€¥‘Ì€ôÍ•Ð ¤((€€€€€€€¥˜¹½Ð¥‘Ìè(€€€€€€€€€€€¥‘Ì€ô…Ý…¥ÐÍ•±˜¹}…Íå¹}É•…‘}‘…Í¡‰½…É‘}¥‘Ì ¤(€€€€€€€¥˜¹½Ð¥‘Ìè(€€€€€€€€€€€É…¥Í”M••‘‰½á¥Í½Ù•ÉåÉÉ½È (€€€€€€€€€€€€€€€€‰9¼Í••‘‰½à%½Õ±‰”‘¥Í½Ù•É•™É½´Ñ¡¥ÌÍ•ÍÍ¥½¸ˆ(€€€€€€€€€€€€¤(€€€€€€€É•ÑÕÉ¸íÍ••‘‰½á}¥èíô™½ÈÍ••‘‰½á}¥¥¸Í½ÉÑ•¡¥‘Ì¥ô((€€€…Íå¹Œ‘•˜…Íå¹}•Ñ}‘…Ñ„¡Í•±˜¤€´ø‘¥ÑmÍÑÈ°¹åtè(€€€€€€€€ˆˆ‰•Ñ …¹Á…ÉÍ”Ñ¡”…ÕÑ¡•¹Ñ¥…Ñ•‘…Í¡‰½…ÉÁ…”¸ˆˆˆ(€€€€€€€¥˜Í•±˜¹}Í••‘‰½á}¥¥Ì9½¹”è(€€€€€€€€€€€É…¥Í”M••‘‰½á…Ñ…ÉÉ½È ‰9¼Í••‘‰½à¥ÌÍ•±•Ñ•ˆ¤(€€€€€€€ÕÉ°€ô˜‰í	M}UI1ô½‘…Í¡‰½…É½Í••‘‰½á•Ì½íÍ•±˜¹}Í••‘‰½á}¥‘ôˆ((€€€€€€€…Íå¹ŒÝ¥Ñ Í•±˜¹}Í•ÍÍ¥½¸¹•Ð (€€€€€€€€€€€ÕÉ°°¡•…‘•ÉÌõÍ•±˜¹}¡•…‘•ÉÌ ¤°…±±½Ý}É•‘¥É•ÑÌõ…±Í”(€€€€€€€€¤…ÌÉ•ÍÁ½¹Í”è(€€€€€€€€€€€¥˜É•ÍÁ½¹Í”¹ÍÑ…ÑÕÌ¥¸MMM%=9}aA%I}MQQUMLè(€€€€€€€€€€€€€€€É…¥Í”M••‘‰½áM•ÍÍ¥½¹áÁ¥É•‘ÉÉ½È ‰M•ÍÍ¥½¸½½­¥”¥Ì¥¹Ù…±¥½È•áÁ¥É•ˆ¤(€€€€€€€€€€€¥˜É•ÍÁ½¹Í”¹ÍÑ…ÑÕÌ€ôô€ÐÀÐè(€€€€€€€€€€€€€€€É…¥Í”M••‘‰½áÕÑ¡•¹Ñ¥…Ñ¥½¹ÉÉ½È (€€€€€€€€€€€€€€€€€€€€‰Q¡”É•ÅÕ•ÍÑ•Í••‘‰½à¥Ì¹½Ð…Ù…¥±…‰±”¥¸Ñ¡¥ÌÍ•ÍÍ¥½¸ˆ(€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€¥˜É•ÍÁ½¹Í”¹ÍÑ…ÑÕÌ€„ô€ÈÀÀè(€€€€€€€€€€€€€€€É…¥Í”M••‘‰½á…Ñ…ÉÉ½È¡˜‰…Í¡‰½…ÉÉ•ÑÕÉ¹•!QQ@íÉ•ÍÁ½¹Í”¹ÍÑ…ÑÕÍôˆ¤(€€€€€€€€€€€É•Á±…•µ•¹Ñ}½½­¥”€ôÍ•±˜¹}É•ÍÁ½¹Í•}Í•ÍÍ¥½¹}½½­¥”¡É•ÍÁ½¹Í”¤(€€€€€€€€€€€¡Ñµ°€ô…Ý…¥ÐÉ•ÍÁ½¹Í”¹Ñ•áÐ ¤((€€€€€€€¥˜˜qp‰Í••‘‰½á%‘qpˆéqp‰íÍ•±˜¹}Í••‘‰½á}¥‘õqpˆœ¹½Ð¥¸¡Ñµ°è(€€€€€€€€€€€¥˜Í•±˜¹}±½½­Í}±¥­•}…ÕÑ¡•¹Ñ¥…Ñ¥½¹}Á…”¡¡Ñµ°¤è(€€€€€€€€€€€€€€€É…¥Í”M••‘‰½áM•ÍÍ¥½¹áÁ¥É•‘ÉÉ½È ‰M•ÍÍ¥½¸½½­¥”¥Ì¥¹Ù…±¥½È•áÁ¥É•ˆ¤(€€€€€€€€€€€É…¥Í”M••‘‰½á…Ñ…ÉÉ½È (€€€€€€€€€€€€€€€€‰…Í¡‰½…ÉÉ•ÍÁ½¹Í”‘¥¹½Ð½¹Ñ…¥¸Ñ¡”Í•±•Ñ•Í••‘‰½àˆ(€€€€€€€€€€€€¤((€€€€€€€‘¥Í­}Í¥é”€ô™±½…Ð¡Í•±˜¹}•áÑÉ…Ñ}¹Õµ‰•È¡¡Ñµ°°€‰‘¥Í­MÁ…•1¥µ¥Ðˆ¤¤(€€€€€€€ÑÉ…™™¥}É…Ü€ô™±½…Ð¡Í•±˜¹}•áÑÉ…Ñ}¹Õµ‰•È¡¡Ñµ°°€‰ÕÉÉ•¹Ñ5½¹Ñ¡QÉ…™™¥Œˆ¤¤(€€€€€€€µ•ÑÉ¥Ì€ôÉ”¹™¥¹‘…±° (€€€€€€€€€€€Èqp‰‘¥Í­ÍÁ…•qpˆè¡q¬¤±qp‰ÑÉ…™™¥qpˆè¡q¬¤œ°(€€€€€€€€€€€¡Ñµ°°(€€€€€€€€¤(€€€€€€€¥˜¹½Ðµ•ÑÉ¥Ìè(€€€€€€€€€€€É…¥Í”M••‘‰½á…Ñ…ÉÉ½È ‰9¼Ñ•±•µ•ÑÉäµ•ÑÉ¥Ì™½Õ¹¥¸‘…Í¡‰½…ÉÁ…”ˆ¤((€€€€€€€‘¥Í­}ÕÍ•‘}µˆ€ô™±½…Ð¡µ•ÑÉ¥Íl´ÅulÁt¤(€€€€€€€‘¥Í­}ÕÍ•‘}ˆ€ôÉ½Õ¹¡‘¥Í­}ÕÍ•‘}µˆ€¼€ÄÀÀÀ°€È¤(€€€€€€€‘¥Í­}™É••}ˆ€ôÉ½Õ¹¡µ…à¡‘¥Í­}Í¥é”€´‘¥Í­}ÕÍ•‘}ˆ°€À¤°€È¤(€€€€€€€‘¥Í­}ÕÍ•‘}ÁÐ€ôÉ½Õ¹ ¡‘¥Í­}ÕÍ•‘}ˆ€¼‘¥Í­}Í¥é”¤€¨€ÄÀÀ°€È¤¥˜‘¥Í­}Í¥é”•±Í”€À((€€€€€€€É•ÍÕ±Ð€ôì(€€€€€€€€€€€€‰‘…Ñ„ˆèì(€€€€€€€€€€€€€€€95}%M-}EU=Q}Iè‘¥Í­}™É••}ˆ°(€€€€€€€€€€€€€€€95}%M-}EU=Q}UMè‘¥Í­}ÕÍ•‘}ˆ°(€€€€€€€€€€€€€€€95}%M-}EU=Q}UM}APè‘¥Í­}ÕÍ•‘}ÁÐ°(€€€€€€€€€€€€€€€95}5=9Q!1e}QI%èÉ½Õ¹¡ÑÉ…™™¥}É…Ü€¼€ÄÀÈÐ°€È¤°(€€€€€€€€€€€€€€€95}%M-}M%iè‘¥Í­}Í¥é”°(€€€€€€€€€€€€€€€95}%A}IMLèÍ•±˜¹}•áÑÉ…Ñ}Ñ…‰±•}Ù…±Õ”¡¡Ñµ°°€‰M•ÉÙ•È%@ˆ¤°(€€€€€€€€€€€€€€€95}Q=II9Q}1%9PèÍ•±˜¹}•áÑÉ…Ñ}½ÁÑ¥½¹…±}Ñ…‰±•}Ù…±Õ” (€€€€€€€€€€€€€€€€€€€¡Ñµ°°€‰Q½ÉÉ•¹Ð±¥•¹Ðˆ(€€€€€€€€€€€€€€€€¤°(€€€€€€€€€€€€€€€95}MQQULèÍ•±˜¹}•áÑÉ…Ñ}Ñ…‰±•}Ù…±Õ”¡¡Ñµ°°€‰MÑ…ÑÕÌˆ¤°(€€€€€€€€€€€ô(€€€€€€€ô(€€€€€€€¥˜É•Á±…•µ•¹Ñ}½½­¥”¥Ì¹½Ð9½¹”è(€€€€€€€€€€€Í•±˜¹}Í•ÍÍ¥½¹}½½­¥”€ôÉ•Á±…•µ•¹Ñ}½½­¥”(€€€€€€€É•ÑÕÉ¸É•ÍÕ±Ð((€€€…Íå¹Œ‘•˜}…Íå¹}É•…‘}Ñ•±•µ•ÑÉå}¥‘Ì¡Í•±˜¤€´øÍ•ÑmÍÑÉtè(€€€€€€€€ˆˆ‰I•…Í••‘‰½à¥‘•¹Ñ¥™¥•ÉÌ™É½´Ñ¡”…ÕÑ¡•¹Ñ¥…Ñ•MMÍÑÉ•…´¸ˆˆˆ(€€€€€€€¥‘ÌèÍ•ÑmÍÑÉt€ôÍ•Ð ¤(€€€€€€€•áÁ•Ñ•è¥¹Ðð9½¹”€ô9½¹”(€€€€€€€É•Á±…•µ•¹Ñ}½½­¥”èÍÑÈð9½¹”€ô9½¹”(€€€€€€€…Íå¹ŒÝ¥Ñ …Íå¹¥¼¹Ñ¥µ•½ÕÐ ÈÀ¤è(€€€€€€€€€€€…Íå¹ŒÝ¥Ñ Í•±˜¹}Í•ÍÍ¥½¸¹•Ð (€€€€€€€€€€€€€€€Q15QIe}UI0°(€€€€€€€€€€€€€€€¡•…‘•ÉÌõÍ•±˜¹}¡•…‘•ÉÌ ‰Ñ•áÐ½•Ù•¹ÐµÍÑÉ•…´ˆ¤°(€€€€€€€€€€€€€€€…±±½Ý}É•‘¥É•ÑÌõ…±Í”°(€€€€€€€€€€€€¤…ÌÉ•ÍÁ½¹Í”è(€€€€€€€€€€€€€€€¥˜É•ÍÁ½¹Í”¹ÍÑ…ÑÕÌ¥¸MMM%=9}aA%I}MQQUMLè(€€€€€€€€€€€€€€€€€€€É…¥Í”M••‘‰½áM•ÍÍ¥½¹áÁ¥É•‘ÉÉ½È (€€€€€€€€€€€€€€€€€€€€€€€€‰M•ÍÍ¥½¸½½­¥”¥Ì¥¹Ù…±¥½È•áÁ¥É•ˆ(€€€€€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€€€€€¥˜É•ÍÁ½¹Í”¹ÍÑ…ÑÕÌ€„ô€ÈÀÀè(€€€€€€€€€€€€€€€€€€€É…¥Í”M••‘‰½á…Ñ…ÉÉ½È (€€€€€€€€€€€€€€€€€€€€€€€˜‰Q•±•µ•ÑÉäÍÑÉ•…´É•ÑÕÉ¹•!QQ@íÉ•ÍÁ½¹Í”¹ÍÑ…ÑÕÍôˆ(€€€€€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€€€€€É•Á±…•µ•¹Ñ}½½­¥”€ôÍ•±˜¹}É•ÍÁ½¹Í•}Í•ÍÍ¥½¹}½½­¥”¡É•ÍÁ½¹Í”¤(€€€€€€€€€€€€€€€…Íå¹Œ™½ÈÉ…Ý}±¥¹”¥¸É•ÍÁ½¹Í”¹½¹Ñ•¹Ðè(€€€€€€€€€€€€€€€€€€€•Ù•¹Ð€ôM••‘‰½á±¥•¹Ð¹}Á…ÉÍ•}ÍÍ•}±¥¹”¡É…Ý}±¥¹”¤(€€€€€€€€€€€€€€€€€€€¥˜•Ù•¹Ð¥Ì9½¹”è(€€€€€€€€€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€€€€€€€€€€€€€¥˜•Ù•¹Ð¹•Ð ‰ÑåÁ”ˆ¤€ôô€‰½¹¹•Ñ•ˆè(€€€€€€€€€€€€€€€€€€€€€€€•áÁ•Ñ•€ô¥¹Ð¡•Ù•¹Ð¹•Ð ‰Í••‘‰½á½Õ¹Ðˆ¤½È€À¤(€€€€€€€€€€€€€€€€€€€€€€€¥˜•áÁ•Ñ•€ôô€Àè(€€€€€€€€€€€€€€€€€€€€€€€€€€€‰É•…¬(€€€€€€€€€€€€€€€€€€€¥˜•Ù•¹Ð¹•Ð ‰Í••‘‰½á%ˆ¤¥Ì¹½Ð9½¹”è(€€€€€€€€€€€€€€€€€€€€€€€…¹‘¥‘…Ñ”€ôÍÑÈ¡•Ù•¹Ñl‰Í••‘‰½á%‰t¤(€€€€€€€€€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€€€€€€€€€¥‘Ì¹…‘¡Ù…±¥‘…Ñ•}Í••‘‰½á}¥¡…¹‘¥‘…Ñ”¤¤(€€€€€€€€€€€€€€€€€€€€€€€•á•ÁÐY…±Õ•ÉÉ½Èè(€€€€€€€€€€€€€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€€€€€€€€€€€€€¥˜•áÁ•Ñ•¥Ì¹½Ð9½¹”…¹±•¸¡¥‘Ì¤€øô•áÁ•Ñ•è(€€€€€€€€€€€€€€€€€€€€€€€‰É•…¬(€€€€€€€¥˜¥‘Ì…¹É•Á±…•µ•¹Ñ}½½­¥”¥Ì¹½Ð9½¹”è(€€€€€€€€€€€Í•±˜¹}Í•ÍÍ¥½¹}½½­¥”€ôÉ•Á±…•µ•¹Ñ}½½­¥”(€€€€€€€É•ÑÕÉ¸¥‘Ì((€€€…Íå¹Œ‘•˜}…Íå¹}É•…‘}‘…Í¡‰½…É‘}¥‘Ì¡Í•±˜¤€´øÍ•ÑmÍÑÉtè(€€€€€€€€ˆˆ‰¥¹Í••‘‰½à¥‘•¹Ñ¥™¥•ÉÌ¥¸Ñ¡”…ÕÑ¡•¹Ñ¥…Ñ•‘…Í¡‰½…É!Q50¸ˆˆˆ(€€€€€€€…Íå¹ŒÝ¥Ñ Í•±˜¹}Í•ÍÍ¥½¸¹•Ð (€€€€€€€€€€€˜‰í	M}UI1ô½‘…Í¡‰½…Éˆ°(€€€€€€€€€€€¡•…‘•ÉÌõÍ•±˜¹}¡•…‘•ÉÌ ¤°(€€€€€€€€€€€…±±½Ý}É•‘¥É•ÑÌõ…±Í”°(€€€€€€€€¤…ÌÉ•ÍÁ½¹Í”è(€€€€€€€€€€€¥˜É•ÍÁ½¹Í”¹ÍÑ…ÑÕÌ¥¸MMM%=9}aA%I}MQQUMLè(€€€€€€€€€€€€€€€É…¥Í”M••‘‰½áM•ÍÍ¥½¹áÁ¥É•‘ÉÉ½È ‰M•ÍÍ¥½¸½½­¥”¥Ì¥¹Ù…±¥½È•áÁ¥É•ˆ¤(€€€€€€€€€€€¥˜É•ÍÁ½¹Í”¹ÍÑ…ÑÕÌ€„ô€ÈÀÀè(€€€€€€€€€€€€€€€É…¥Í”M••‘‰½á…Ñ…ÉÉ½È¡˜‰…Í¡‰½…ÉÉ•ÑÕÉ¹•!QQ@íÉ•ÍÁ½¹Í”¹ÍÑ…ÑÕÍôˆ¤(€€€€€€€€€€€É•Á±…•µ•¹Ñ}½½­¥”€ôÍ•±˜¹}É•ÍÁ½¹Í•}Í•ÍÍ¥½¹}½½­¥”¡É•ÍÁ½¹Í”¤(€€€€€€€€€€€¡Ñµ°€ô…Ý…¥ÐÉ•ÍÁ½¹Í”¹Ñ•áÐ ¤((€€€€€€€¹½Éµ…±¥é•‘}¡Ñµ°€ô¡Ñµ°¹É•Á±…” qpˆœ°€œˆœ¤(€€€€€€€…¹‘¥‘…Ñ•Ì€ôÍ•Ð¡É”¹™¥¹‘…±°¡Èœ‰Í••‘‰½á%‰qÌ¨éqÌ¨ˆ¡q¬¤ˆœ°¹½Éµ…±¥é•‘}¡Ñµ°¤¤(€€€€€€€…¹‘¥‘…Ñ•Ì¹ÕÁ‘…Ñ”¡É”¹™¥¹‘…±°¡Èˆ½‘…Í¡‰½…É½Í••‘‰½á•Ì¼¡q¬¤ˆ°¹½Éµ…±¥é•‘}¡Ñµ°¤¤(€€€€€€€¥‘ÌèÍ•ÑmÍÑÉt€ôÍ•Ð ¤(€€€€€€€™½È…¹‘¥‘…Ñ”¥¸…¹‘¥‘…Ñ•Ìè(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€¥‘Ì¹…‘¡Ù…±¥‘…Ñ•}Í••‘‰½á}¥¡…¹‘¥‘…Ñ”¤¤(€€€€€€€€€€€•á•ÁÐY…±Õ•ÉÉ½Èè(€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€¥˜¥‘Ì…¹É•Á±…•µ•¹Ñ}½½­¥”¥Ì¹½Ð9½¹”è(€€€€€€€€€€€Í•±˜¹}Í•ÍÍ¥½¹}½½­¥”€ôÉ•Á±…•µ•¹Ñ}½½­¥”(€€€€€€€É•ÑÕÉ¸¥‘Ì((€€€‘•˜}¡•…‘•ÉÌ¡Í•±˜°…•ÁÐèÍÑÈ€ô€‰Ñ•áÐ½¡Ñµ°ˆ¤€´ø‘¥ÑmÍÑÈ°ÍÑÉtè(€€€€€€€€ˆˆ‰I•ÑÕÉ¸¡•…‘•ÉÌ™½È…¸…ÕÑ¡•¹Ñ¥…Ñ•‰É½ÝÍ•ÈµÍ•ÍÍ¥½¸É•ÅÕ•ÍÐ¸ˆˆˆ(€€€€€€€É•ÑÕÉ¸ì(€€€€€€€€€€€€‰½½­¥”ˆè˜‰Í•ÍÍ¥½¹}¥õíÍ•±˜¹}Í•ÍÍ¥½¹}½½­¥•ôˆ°(€€€€€€€€€€€€‰UÍ•Èµ•¹ÐˆèUMI}9P°(€€€€€€€€€€€€‰•ÁÐˆè…•ÁÐ°(€€€€€€€ô((€€€ÍÑ…Ñ¥µ•Ñ¡½(€€€‘•˜}É•ÍÁ½¹Í•}Í•ÍÍ¥½¹}½½­¥” (€€€€€€€É•ÍÁ½¹Í”è…¥½¡ÑÑÀ¹±¥•¹ÑI•ÍÁ½¹Í”°(€€€€¤€´øÍÑÈð9½¹”è(€€€€€€€€ˆˆ‰I•…„É½Ñ…Ñ•Í•ÍÍ¥½¹}¥½¹±ä™É½´…¸…ÕÑ¡•¹Ñ¥…Ñ•Í¥Ñ”É•ÍÁ½¹Í”¸ˆˆˆ(€€€€€€€¥˜ÕÉ±Á…ÉÍ”¡ÍÑÈ¡É•ÍÁ½¹Í”¹ÕÉ°¤¤¹¡½ÍÑ¹…µ”€„ô€‰ÝÝÜ¹Í••‘‰½á•Ì¹Œˆè(€€€€€€€€€€€É•ÑÕÉ¸9½¹”(€€€€€€€Í•ÍÍ¥½¹}½½­¥”€ôÉ•ÍÁ½¹Í”¹½½­¥•Ì¹•Ð ‰Í•ÍÍ¥½¹}¥ˆ¤(€€€€€€€¥˜Í•ÍÍ¥½¹}½½­¥”¥Ì9½¹”è(€€€€€€€€€€€É•ÑÕÉ¸9½¹”(€€€€€€€µ…á}…”€ôÍ•ÍÍ¥½¹}½½­¥•l‰µ…àµ…”‰t(€€€€€€€¥˜µ…á}…”è(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€¥˜¥¹Ð¡µ…á}…”¤€ðô€Àè(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸9½¹”(€€€€€€€€€€€•á•ÁÐY…±Õ•ÉÉ½Èè(€€€€€€€€€€€€€€€É•ÑÕÉ¸9½¹”(€€€€€€€•áÁ¥É•Ì€ôÍ•ÍÍ¥½¹}½½­¥•l‰•áÁ¥É•Ì‰t(€€€€€€€¥˜•áÁ¥É•Ìè(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€•áÁ¥É•Í}…Ð€ôÁ…ÉÍ•‘…Ñ•}Ñ½}‘…Ñ•Ñ¥µ”¡•áÁ¥É•Ì¤(€€€€€€€€€€€€€€€¥˜•áÁ¥É•Í}…Ð¹Ñé¥¹™¼¥Ì9½¹”è(€€€€€€€€€€€€€€€€€€€•áÁ¥É•Í}…Ð€ô•áÁ¥É•Í}…Ð¹É•Á±…”¡Ñé¥¹™¼õÑ¥µ•é½¹”¹ÕÑŒ¤(€€€€€€€€€€€€€€€¥˜•áÁ¥É•Í}…Ð€ðô‘…Ñ•Ñ¥µ”¹¹½Ü¡Ñ¥µ•é½¹”¹ÕÑŒ¤è(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸9½¹”(€€€€€€€€€€€•á•ÁÐ€¡QåÁ•ÉÉ½È°Y…±Õ•ÉÉ½È°=Ù•É™±½ÝÉÉ½È¤è(€€€€€€€€€€€€€€€É•ÑÕÉ¸9½¹”(€€€€€€€ÑÉäè(€€€€€€€€€€€É•ÑÕÉ¸Ù…±¥‘…Ñ•}Í•ÍÍ¥½¹}½½­¥”¡Í•ÍÍ¥½¹}½½­¥”¹Ù…±Õ”¤(€€€€€€€•á•ÁÐY…±Õ•ÉÉ½Èè(€€€€€€€€€€€É•ÑÕÉ¸9½¹”((€€€ÍÑ…Ñ¥µ•Ñ¡½(€€€‘•˜}±½½­Í}±¥­•}…ÕÑ¡•¹Ñ¥…Ñ¥½¹}Á…”¡¡Ñµ°èÍÑÈ¤€´ø‰½½°è(€€€€€€€€ˆˆ‰I•ÑÕÉ¸Ý¡•Ñ¡•È„ÍÕ•ÍÍ™Õ°!QQ@Á…”¥Ì…ÑÕ…±±ä„±½¥¸ÍÉ••¸¸ˆˆˆ(€€€€€€€±½Ý•É}¡Ñµ°€ô¡Ñµ°¹±½Ý•È ¤(€€€€€€€¡…Í}±½¥¹}™½É´€ô€ˆñ™½É´ˆ¥¸±½Ý•É}¡Ñµ°…¹€ (€€€€€€€€€€€€ˆ½±½¥¸µ…Ñ¥½¹Ì¼ˆ¥¸±½Ý•É}¡Ñµ°(€€€€€€€€€€€½È€¹…µ”ô‰Á…ÍÍÝ½Éˆœ¥¸±½Ý•É}¡Ñµ°(€€€€€€€€€€€½È€‰¹…µ”ôÁ…ÍÍÝ½Éœˆ¥¸±½Ý•É}¡Ñµ°(€€€€€€€€¤(€€€€€€€¡…Í}‰É½ÝÍ•É}¡…±±•¹”€ô…¹ä (€€€€€€€€€€€µ…É­•È¥¸±½Ý•É}¡Ñµ°™½Èµ…É­•È¥¸€ ‰˜µÑÕÉ¹ÍÑ¥±”ˆ°€‰˜µ¡°´ˆ¤(€€€€€€€€¤(€€€€€€€É•ÑÕÉ¸¡…Í}±½¥¹}™½É´½È¡…Í}‰É½ÝÍ•É}¡…±±•¹”((€€€ÍÑ…Ñ¥µ•Ñ¡½(€€€‘•˜}•áÑÉ…Ñ}¹Õµ‰•È¡¡Ñµ°èÍÑÈ°­•äèÍÑÈ¤€´øÍÑÈè(€€€€€€€µ…Ñ €ôÉ”¹Í•…É  (€€€€€€€€€€€É˜qp‰íÉ”¹•Í…Á”¡­•ä¥õqpˆè¡q¬ üép¹q¬¤ü¤œ°(€€€€€€€€€€€¡Ñµ°°(€€€€€€€€¤(€€€€€€€¥˜¹½Ðµ…Ñ è(€€€€€€€€€€€É…¥Í”M••‘‰½á…Ñ…ÉÉ½È¡˜‰5¥ÍÍ¥¹œ‘…Í¡‰½…ÉÙ…±Õ”èí­•åôˆ¤(€€€€€€€É•ÑÕÉ¸µ…Ñ ¹É½ÕÀ Ä¤((€€€ÍÑ…Ñ¥µ•Ñ¡½(€€€‘•˜}•áÑÉ…Ñ}Ñ…‰±•}Ù…±Õ”¡¡Ñµ°èÍÑÈ°±…‰•°èÍÑÈ¤€´øÍÑÈè(€€€€€€€Á…ÑÑ•É¸€ô€ (€€€€€€€€€€€É˜qp‰¡¥±‘É•¹qpˆéqp‰íÉ”¹•Í…Á”¡±…‰•°¥õqpˆœ(€€€€€€€€€€€É˜œ¹íìÀ°ÄÐÀÁõôýqp‰¡¥±‘É•¹qpˆéqpˆ¡myqp‰t¬¥qpˆœ(€€€€€€€€¤(€€€€€€€µ…Ñ €ôÉ”¹Í•…É ¡Á…ÑÑ•É¸°¡Ñµ°¤(€€€€€€€¥˜¹½Ðµ…Ñ è(€€€€€€€€€€€É…¥Í”M••‘‰½á…Ñ…ÉÉ½È¡˜‰5¥ÍÍ¥¹œ‘…Í¡‰½…É™¥•±èí±…‰•±ôˆ¤(€€€€€€€É•ÑÕÉ¸µ…Ñ ¹É½ÕÀ Ä¤((€€€±…ÍÍµ•Ñ¡½(€€€‘•˜}•áÑÉ…Ñ}½ÁÑ¥½¹…±}Ñ…‰±•}Ù…±Õ”¡±Ì°¡Ñµ°èÍÑÈ°±…‰•°èÍÑÈ¤€´øÍÑÈð9½¹”è(€€€€€€€ÑÉäè(€€€€€€€€€€€É•ÑÕÉ¸±Ì¹}•áÑÉ…Ñ}Ñ…‰±•}Ù…±Õ”¡¡Ñµ°°±…‰•°¤(€€€€€€€•á•ÁÐM••‘‰½á…Ñ…ÉÉ½Èè(€€€€€€€€€€€É•ÑÕÉ¸9½¹”(()±…ÍÌ!å‰É¥‘M••‘‰½á±¥•¹Ðè(€€€€ˆˆ‰UÍ”„Í…Ù•½½­¥”™¥ÉÍÐ…¹É•¹•Ü¥Ð½¹”Ý¥Ñ …½Õ¹ÐÉ•‘•¹Ñ¥…±Ì¸ˆˆˆ((€€€‘•˜}}¥¹¥Ñ}| (€€€€€€€Í•±˜°(€€€€€€€Í•ÍÍ¥½¸è…¥½¡ÑÑÀ¹±¥•¹ÑM•ÍÍ¥½¸°(€€€€€€€Í••‘‰½á}¥èÍÑÈ°(€€€€€€€ÕÍ•É¹…µ”èÍÑÈð9½¹”°(€€€€€€€Á…ÍÍÝ½ÉèÍÑÈð9½¹”°(€€€€€€€Í•ÍÍ¥½¹}½½­¥”èÍÑÈð9½¹”°(€€€€¤€´ø9½¹”è(€€€€€€€Í•±˜¹}Í•ÍÍ¥½¸€ôÍ•ÍÍ¥½¸(€€€€€€€Í•±˜¹}Í••‘‰½á}¥€ôÙ…±¥‘…Ñ•}Í••‘‰½á}¥¡Í••‘‰½á}¥¤(€€€€€€€Í•±˜¹}ÕÍ•É¹…µ”€ôÕÍ•É¹…µ”¹ÍÑÉ¥À ¤¥˜ÕÍ•É¹…µ”•±Í”9½¹”(€€€€€€€Í•±˜¹}Á…ÍÍÝ½É€ôÁ…ÍÍÝ½É¥˜Á…ÍÍÝ½É•±Í”9½¹”(€€€€€€€Í•±˜¹}½½­¥•}±¥•¹Ð€ô€ (€€€€€€€€€€€M•ÍÍ¥½¹½½­¥•M••‘‰½á±¥•¹Ð (€€€€€€€€€€€€€€€Í•ÍÍ¥½¸°(€€€€€€€€€€€€€€€Í•±˜¹}Í••‘‰½á}¥°(€€€€€€€€€€€€€€€Í•ÍÍ¥½¹}½½­¥”°(€€€€€€€€€€€€¤(€€€€€€€€€€€¥˜Í•ÍÍ¥½¹}½½­¥”(€€€€€€€€€€€•±Í”9½¹”(€€€€€€€€¤(€€€€€€€Í•±˜¹}É•‘•¹Ñ¥…±}±¥•¹Ð€ô€ (€€€€€€€€€€€M••‘‰½á±¥•¹Ð (€€€€€€€€€€€€€€€Í•ÍÍ¥½¸°(€€€€€€€€€€€€€€€Í•±˜¹}Í••‘‰½á}¥°(€€€€€€€€€€€€€€€Í•±˜¹}ÕÍ•É¹…µ”°(€€€€€€€€€€€€€€€Í•±˜¹}Á…ÍÍÝ½É°(€€€€€€€€€€€€¤(€€€€€€€€€€€¥˜Í•±˜¹}ÕÍ•É¹…µ”…¹Í•±˜¹}Á…ÍÍÝ½É(€€€€€€€€€€€•±Í”9½¹”(€€€€€€€€¤(€€€€€€€Í•±˜¹}É•¹•Ý…±}±½¬€ô…Íå¹¥¼¹1½¬ ¤((€€€ÁÉ½Á•ÉÑä(€€€‘•˜Í•ÍÍ¥½¹}½½­¥”¡Í•±˜¤€´øÍÑÈð9½¹”è(€€€€€€€€ˆˆ‰I•ÑÕÉ¸Ñ¡”µ½ÍÐÉ••¹ÐÉ…ÜÍ•ÍÍ¥½¹}¥Ù…±Õ”¸ˆˆˆ(€€€€€€€¥˜Í•±˜¹}½½­¥•}±¥•¹Ð¥Ì¹½Ð9½¹”è(€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}½½­¥•}±¥•¹Ð¹Í•ÍÍ¥½¹}½½­¥”(€€€€€€€¥˜Í•±˜¹}É•‘•¹Ñ¥…±}±¥•¹Ð¥Ì¹½Ð9½¹”è(€€€€€€€€€€€É•ÑÕÉ¸Í•±˜¹}É•‘•¹Ñ¥…±}±¥•¹Ð¹Í•ÍÍ¥½¹}½½­¥”(€€€€€€€É•ÑÕÉ¸9½¹”((€€€…Íå¹Œ‘•˜…Íå¹}Ù…±¥‘…Ñ•}É•‘•¹Ñ¥…±Ì¡Í•±˜¤€´ø9½¹”è(€€€€€€€€ˆˆ‰Y…±¥‘…Ñ”…Ð±•…ÍÐ½¹”½¹™¥ÕÉ•…ÕÑ¡•¹Ñ¥…Ñ¥½¸µ•Ñ¡½¸ˆˆˆ(€€€€€€€…Ý…¥ÐÍ•±˜¹…Íå¹}•Ñ}‘…Ñ„ ¤((€€€…Íå¹Œ‘•˜…Íå¹}•Ñ}‘…Ñ„¡Í•±˜¤€´ø‘¥ÑmÍÑÈ°¹åtè(€€€€€€€€ˆˆ‰•Ñ ‘…Ñ„…¹É•¹•Ü½¹±ä„•¹Õ¥¹•±ä•áÁ¥É•‰É½ÝÍ•ÈÍ•ÍÍ¥½¸¸ˆˆˆ(€€€€€€€¥˜Í•±˜¹}½½­¥•}±¥•¹Ð¥Ì9½¹”è(€€€€€€€€€€€¥˜Í•±˜¹}É•‘•¹Ñ¥…±}±¥•¹Ð¥Ì9½¹”è(€€€€€€€€€€€€€€€É…¥Í”M••‘‰½áÕÑ¡•¹Ñ¥…Ñ¥½¹ÉÉ½È (€€€€€€€€€€€€€€€€€€€€‰½Õ¹ÐÉ•‘•¹Ñ¥…±Ì½È„Í•ÍÍ¥½¸½½­¥”…É”É•ÅÕ¥É•ˆ(€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€…Íå¹ŒÝ¥Ñ Í•±˜¹}É•¹•Ý…±}±½¬è(€€€€€€€€€€€€€€€¥˜Í•±˜¹}½½­¥•}±¥•¹Ð¥Ì¹½Ð9½¹”è(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸…Ý…¥ÐÍ•±˜¹}½½­¥•}±¥•¹Ð¹…Íå¹}•Ñ}‘…Ñ„ ¤(€€€€€€€€€€€€€€€É•ÑÕÉ¸…Ý…¥ÐÍ•±˜¹}…Íå¹}•Ñ}Ý¥Ñ¡}É•‘•¹Ñ¥…±Ì ¤((€€€€€€€•áÁ¥É•‘}½½­¥”€ôÍ•±˜¹}½½­¥•}±¥•¹Ð¹Í•ÍÍ¥½¹}½½­¥”(€€€€€€€ÑÉäè(€€€€€€€€€€€É•ÑÕÉ¸…Ý…¥ÐÍ•±˜¹}½½­¥•}±¥•¹Ð¹…Íå¹}•Ñ}‘…Ñ„ ¤(€€€€€€€•á•ÁÐM••‘‰½áM•ÍÍ¥½¹áÁ¥É•‘ÉÉ½Èè(€€€€€€€€€€€¥˜Í•±˜¹}É•‘•¹Ñ¥…±}±¥•¹Ð¥Ì9½¹”è(€€€€€€€€€€€€€€€É…¥Í”((€€€€€€€…Íå¹ŒÝ¥Ñ Í•±˜¹}É•¹•Ý…±}±½¬è(€€€€€€€€€€€¥˜€ (€€€€€€€€€€€€€€€Í•±˜¹}½½­¥•}±¥•¹Ð¥Ì¹½Ð9½¹”(€€€€€€€€€€€€€€€…¹Í•±˜¹}½½­¥•}±¥•¹Ð¹Í•ÍÍ¥½¹}½½­¥”€„ô•áÁ¥É•‘}½½­¥”(€€€€€€€€€€€€¤è(€€€€€€€€€€€€€€€É•ÑÕÉ¸…Ý…¥ÐÍ•±˜¹}½½­¥•}±¥•¹Ð¹…Íå¹}•Ñ}‘…Ñ„ ¤(€€€€€€€€€€€É•ÑÕÉ¸…Ý…¥ÐÍ•±˜¹}…Íå¹}•Ñ}Ý¥Ñ¡}É•‘•¹Ñ¥…±Ì ¤((€€€…Íå¹Œ‘•˜}…Íå¹}•Ñ}Ý¥Ñ¡}É•‘•¹Ñ¥…±Ì¡Í•±˜¤€´ø‘¥ÑmÍÑÈ°¹åtè(€€€€€€€€ˆˆ‰A•É™½É´½¹”É•‘•¹Ñ¥…°µ‰…­•ÕÁ‘…Ñ”…¹…‘½ÁÐ¥ÑÌ¹•Ü½½­¥”¸ˆˆˆ(€€€€€€€¥˜Í•±˜¹}É•‘•¹Ñ¥…±}±¥•¹Ð¥Ì9½¹”è(€€€€€€€€€€€É…¥Í”M••‘‰½áÕÑ¡•¹Ñ¥…Ñ¥½¹ÉÉ½È (€€€€€€€€€€€€€€€€‰½Õ¹ÐÉ•‘•¹Ñ¥…±Ì…É”É•ÅÕ¥É•Ñ¼É•¹•ÜÑ¡”Í•ÍÍ¥½¸ˆ(€€€€€€€€€€€€¤(€€€€€€€…Ý…¥ÐÍ•±˜¹}É•‘•¹Ñ¥…±}±¥•¹Ð¹…Íå¹}•Ñ}‘…Ñ„ ¤(€€€€€€€Í•ÍÍ¥½¹}½½­¥”€ôÍ•±˜¹}É•‘•¹Ñ¥…±}±¥•¹Ð¹Í•ÍÍ¥½¹}½½­¥”(€€€€€€€¥˜Í•ÍÍ¥½¹}½½­¥”¥Ì9½¹”è(€€€€€€€€€€€É…¥Í”M••‘‰½áÕÑ¡•¹Ñ¥…Ñ¥½¹ÉÉ½È (€€€€€€€€€€€€€€€€‰M••‘‰½á•Ì¹Œ‘¥¹½ÐÁÉ½Ù¥‘”…¸…ÕÑ¡•¹Ñ¥…Ñ•Í•ÍÍ¥½¸ˆ(€€€€€€€€€€€€¤(€€€€€€€Í•±˜¹}½½­¥•}±¥•¹Ð€ôM•ÍÍ¥½¹½½­¥•M••‘‰½á±¥•¹Ð (€€€€€€€€€€€Í•±˜¹}Í•ÍÍ¥½¸°(€€€€€€€€€€€Í•±˜¹}Í••‘‰½á}¥°(€€€€€€€€€€€Í•ÍÍ¥½¹}½½­¥”°(€€€€€€€€¤(€€€€€€€É•ÑÕÉ¸…Ý…¥ÐÍ•±˜¹}½½­¥•}±¥•¹Ð¹…Íå¹}•Ñ}‘…Ñ„ ¤(()Í••‘‰½á}±¥•¹Ð€ôM••‘‰½á±¥•¹Ð(
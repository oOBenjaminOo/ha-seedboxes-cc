"""Tests for the Seedboxes.cc authentication clients."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

import pytest
from yarl import URL

COMPONENT_DIR = Path(__file__).parents[1] / "custom_components" / "seedboxes_cc"
SENSOR_NAMES = (
    "NAME_DISK_QUOTA_FREE",
    "NAME_DISK_QUOTA_USED",
    "NAME_DISK_QUOTA_USED_PCT",
    "NAME_DISK_SIZE",
    "NAME_IP_ADDRESS",
    "NAME_MONTHLY_TRAFFIC",
    "NAME_STATUS",
    "NAME_TORRENT_CLIENT",
)


@pytest.fixture
def client_module():
    """Load the client without importing Home Assistant."""
    package_name = "custom_components.seedboxes_cc"
    package = types.ModuleType(package_name)
    package.__path__ = [str(COMPONENT_DIR)]
    sys.modules[package_name] = package

    const_module = types.ModuleType(f"{package_name}.const")
    for name in SENSOR_NAMES:
        setattr(const_module, name, name)
    sys.modules[const_module.__name__] = const_module

    module_name = f"{package_name}.seedbox_client"
    spec = importlib.util.spec_from_file_location(
        module_name,
        COMPONENT_DIR / "seedbox_client.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class AsyncLines:
    """Small asynchronous line iterator for fake SSE responses."""

    def __init__(self, lines: list[bytes] | None = None) -> None:
        self._lines = iter(lines or [])

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._lines)
        except StopIteration as err:
            raise StopAsyncIteration from err


class FakeResponse:
    """Minimal aiohttp response used by the client tests."""

    def __init__(
        self,
        status: int,
        *,
        url: str = "https://www.seedboxes.cc/dashboard/seedboxes/80414",
        text: str = "",
        cookie: str | None = None,
        cookie_attributes: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        lines: list[bytes] | None = None,
    ) -> None:
        self.status = status
        self.url = URL(url)
        self._text = text
        self.headers = headers or {}
        self.cookies: SimpleCookie[str] = SimpleCookie()
        if cookie is not None:
            self.cookies["session_id"] = cookie
            for key, value in (cookie_attributes or {}).items():
                self.cookies["session_id"][key] = value
        self.content = AsyncLines(lines)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def text(self) -> str:
        return self._text


class FakeSession:
    """Return queued responses and record requests."""

    def __init__(self, *responses: FakeResponse) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def get(self, url: str, **kwargs):
        self.requests.append(("GET", url, kwargs))
        return self.responses.pop(0)

    def request(self, method: str, url: str, **kwargs):
        self.requests.append((method, url, kwargs))
        return self.responses.pop(0)


def dashboard_html(seedbox_id: str = "80414") -> str:
    """Return the minimum dashboard structure accepted by the parser."""
    return (
        rf"\"seedboxId\":\"{seedbox_id}\""
        r"\"diskSpaceLimit\":1000"
        r"\"currentMonthTraffic\":2048"
        r"\"diskspace\":100000,\"traffic\":1"
        r"\"children\":\"Server IP\"x\"children\":\"10.0.0.1\""
        r"\"children\":\"Torrent Client\"x\"children\":\"qBittorrent\""
        r"\"children\":\"Status\"x\"children\":\"Active\""
    )


@pytest.mark.asyncio
async def test_valid_cookie_does_not_login(client_module, monkeypatch):
    """A valid saved cookie remains the preferred authentication method."""
    response = FakeResponse(200, text=dashboard_html())
    session = FakeSession(response)

    class CredentialClient:
        calls = 0

        def __init__(self, *_args):
            self.session_cookie = None

        async def async_get_data(self):
            type(self).calls += 1
            raise AssertionError("Credential login must not run")

    monkeypatch.setattr(client_module, "SeedboxClient", CredentialClient)
    client = client_module.HybridSeedboxClient(
        session, "80414", "account", "password", "saved-cookie"
    )

    result = await client.async_get_data()

    assert result["data"]["NAME_STATUS"] == "Active"
    assert CredentialClient.calls == 0


@pytest.mark.asyncio
async def test_expired_cookie_reauthenticates_once(client_module, monkeypatch):
    """A session-expiry response triggers one credential-backed renewal."""
    session = FakeSession(
        FakeResponse(302),
        FakeResponse(200, text=dashboard_html()),
    )

    class CredentialClient:
        calls = 0

        def __init__(self, *_args):
            self.session_cookie = None

        async def async_get_data(self):
            type(self).calls += 1
            self.session_cookie = "fresh-cookie"
            return {"data": {"NAME_STATUS": "Active"}}

    monkeypatch.setattr(client_module, "SeedboxClient", CredentialClient)
    client = client_module.HybridSeedboxClient(
        session, "80414", "account", "password", "expired-cookie"
    )

    result = await client.async_get_data()

    assert result["data"]["NAME_STATUS"] == "Active"
    assert client.session_cookie == "fresh-cookie"
    assert CredentialClient.calls == 1


@pytest.mark.asyncio
async def test_server_error_does_not_trigger_login(client_module, monkeypatch):
    """A Seedboxes.cc server failure is not mistaken for session expiry."""
    session = FakeSession(FakeResponse(500))

    class CredentialClient:
        calls = 0

        def __init__(self, *_args):
            self.session_cookie = None

        async def async_get_data(self):
            type(self).calls += 1

    monkeypatch.setattr(client_module, "SeedboxClient", CredentialClient)
    client = client_module.HybridSeedboxClient(
        session, "80414", "account", "password", "saved-cookie"
    )

    with pytest.raises(client_module.SeedboxDataError):
        await client.async_get_data()

    assert CredentialClient.calls == 0


@pytest.mark.asyncio
async def test_rotated_cookie_is_adopted_after_valid_data(client_module):
    """A rotated session_id is accepted after the response is validated."""
    session = FakeSession(
        FakeResponse(200, text=dashboard_html(), cookie="rotated-cookie")
    )
    client = client_module.SessionCookieSeedboxClient(session, "80414", "saved-cookie")

    await client.async_get_data()

    assert client.session_cookie == "rotated-cookie"


@pytest.mark.asyncio
async def test_invalid_data_does_not_adopt_rotated_cookie(client_module):
    """A Set-Cookie header alone is not enough to replace saved credentials."""
    session = FakeSession(
        FakeResponse(200, text="invalid dashboard", cookie="untrusted-cookie")
    )
    client = client_module.SessionCookieSeedboxClient(session, "80414", "saved-cookie")

    with pytest.raises(client_module.SeedboxDataError):
        await client.async_get_data()

    assert client.session_cookie == "saved-cookie"


@pytest.mark.asyncio
async def test_http_200_login_page_triggers_one_renewal(client_module, monkeypatch):
    """An expired session can present a recognizable login page with HTTP 200."""
    session = FakeSession(
        FakeResponse(
            200, text='<form action="/login-actions/auth"><input name="password">'
        ),
        FakeResponse(200, text=dashboard_html()),
    )

    class CredentialClient:
        calls = 0

        def __init__(self, *_args):
            self.session_cookie = None

        async def async_get_data(self):
            type(self).calls += 1
            self.session_cookie = "fresh-cookie"
            return {"data": {"NAME_STATUS": "Active"}}

    monkeypatch.setattr(client_module, "SeedboxClient", CredentialClient)
    client = client_module.HybridSeedboxClient(
        session, "80414", "account", "password", "expired-cookie"
    )

    await client.async_get_data()

    assert CredentialClient.calls == 1
    assert client.session_cookie == "fresh-cookie"


@pytest.mark.asyncio
async def test_concurrent_first_refresh_logs_in_once(client_module, monkeypatch):
    """Concurrent credentials-only refreshes share a single renewal."""
    session = FakeSession(
        FakeResponse(200, text=dashboard_html()),
        FakeResponse(200, text=dashboard_html()),
    )
    started = asyncio.Event()
    release = asyncio.Event()

    class CredentialClient:
        calls = 0

        def __init__(self, *_args):
            self.session_cookie = None

        async def async_get_data(self):
            type(self).calls += 1
            started.set()
            await release.wait()
            self.session_cookie = "fresh-cookie"
            return {"data": {"NAME_STATUS": "Active"}}

    monkeypatch.setattr(client_module, "SeedboxClient", CredentialClient)
    client = client_module.HybridSeedboxClient(
        session, "80414", "account", "password", None
    )

    first = asyncio.create_task(client.async_get_data())
    await started.wait()
    second = asyncio.create_task(client.async_get_data())
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)

    assert CredentialClient.calls == 1


@pytest.mark.asyncio
async def test_unknown_http_200_layout_does_not_trigger_login(
    client_module, monkeypatch
):
    """A dashboard markup regression is a data error, not an auth retry."""
    session = FakeSession(
        FakeResponse(
            200,
            text=(
                "<script>const provider='keycloak'; "
                "const login='/api/auth/login'</script>new dashboard layout"
            ),
        )
    )

    class CredentialClient:
        calls = 0

        def __init__(self, *_args):
            self.session_cookie = None

        async def async_get_data(self):
            type(self).calls += 1

    monkeypatch.setattr(client_module, "SeedboxClient", CredentialClient)
    client = client_module.HybridSeedboxClient(
        session, "80414", "account", "password", "saved-cookie"
    )

    with pytest.raises(client_module.SeedboxDataError):
        await client.async_get_data()

    assert CredentialClient.calls == 0


@pytest.mark.asyncio
async def test_deleted_cookie_is_not_persisted(client_module):
    """A Set-Cookie revocation cannot replace the last usable session."""
    session = FakeSession(
        FakeResponse(
            200,
            text=dashboard_html(),
            cookie="apparently-valid",
            cookie_attributes={"max-age": "0"},
        )
    )
    client = client_module.SessionCookieSeedboxClient(session, "80414", "saved-cookie")

    await client.async_get_data()

    assert client.session_cookie == "saved-cookie"


@pytest.mark.asyncio
async def test_cookie_discovers_seedbox_id_from_dashboard(client_module):
    """Dashboard links provide a fallback when the telemetry stream fails."""
    session = FakeSession(
        FakeResponse(500, url="https://www.seedboxes.cc/api/telemetry/stream-all"),
        FakeResponse(
            200,
            url="https://www.seedboxes.cc/dashboard",
            text=(
                '<a href="/dashboard/seedboxes/80414">one</a>'
                r"{\"seedboxId\":\"80415\"}"
            ),
        ),
    )
    client = client_module.SessionCookieSeedboxClient(session, None, "saved-cookie")

    discovered = await client.async_discover_seedboxes()

    assert set(discovered) == {"80414", "80415"}


@pytest.mark.asyncio
async def test_authenticated_page_without_ids_uses_manual_fallback_error(
    client_module,
):
    """Only an authenticated 200 response can signal an ID-discovery fallback."""
    session = FakeSession(
        FakeResponse(500, url="https://www.seedboxes.cc/api/telemetry/stream-all"),
        FakeResponse(200, url="https://www.seedboxes.cc/dashboard", text="dashboard"),
    )
    client = client_module.SessionCookieSeedboxClient(session, None, "saved-cookie")

    with pytest.raises(client_module.SeedboxDiscoveryError):
        await client.async_discover_seedboxes()


@pytest.mark.asyncio
async def test_post_307_redirect_is_rejected(client_module):
    """Credentials are never replayed through a 307/308 redirect."""
    response = FakeResponse(
        307,
        url="https://gatekeeper.seedboxes.cc/login-actions/authenticate",
        headers={"Location": "https://www.seedboxes.cc/dashboard"},
    )
    client = client_module.SeedboxClient(FakeSession(), None, "account", "password")

    with pytest.raises(client_module.SeedboxDataError):
        await client._async_login_request(
            FakeSession(response),
            "POST",
            "https://gatekeeper.seedboxes.cc/login-actions/authenticate",
            {"username": "account", "password": "password"},
        )


@pytest.mark.asyncio
async def test_password_is_submitted_only_once(client_module, monkeypatch):
    """A rejected password form cannot trigger repeated account attempts."""
    form = (
        '<form action="https://gatekeeper.seedboxes.cc/login-actions/auth">'
        '<input name="password" type="password"></form>'
    )
    login_session = FakeSession(
        FakeResponse(
            200,
            url="https://gatekeeper.seedboxes.cc/login",
            text=form,
        ),
        FakeResponse(
            200,
            url="https://gatekeeper.seedboxes.cc/login",
            text=form,
        ),
    )
    monkeypatch.setattr(
        client_module.aiohttp,
        "ClientSession",
        lambda **_kwargs: login_session,
    )
    client = client_module.SeedboxClient(
        FakeSession(), None, "account", "rejected-password"
    )

    with pytest.raises(client_module.SeedboxAuthenticationError):
        await client._async_login(force=True)

    assert [request[0] for request in login_session.requests].count("POST") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 503])
async def test_initial_cloudflare_page_is_diagnosed(
    client_module, monkeypatch, caplog, status
):
    """A protected non-200 login page still produces a sanitized diagnostic."""
    login_session = FakeSession(
        FakeResponse(
            status,
            url="https://www.seedboxes.cc/api/auth/login",
            text="<title>Just a moment</title>Cloudflare Turnstile",
            headers={"Content-Type": "text/html"},
        )
    )
    monkeypatch.setattr(
        client_module.aiohttp,
        "ClientSession",
        lambda **_kwargs: login_session,
    )
    client = client_module.SeedboxClient(FakeSession(), None, "account", "password")

    with (
        caplog.at_level("WARNING"),
        pytest.raises(client_module.SeedboxBrowserVerificationRequired),
    ):
        await client._async_login(force=True)

    assert f"status={status}" in caplog.text
    assert "'cloudflare': true" in caplog.text.lower()


def test_malformed_form_action_cannot_break_diagnostic(client_module, caplog):
    """Malformed URLs are reduced to a safe marker in diagnostics."""
    client = client_module.SeedboxClient(FakeSession(), None, "account", "password")

    with caplog.at_level("WARNING"):
        client._parse_and_log_login_page(
            '<form action="https://[invalid"><input name="username"></form>',
            "https://gatekeeper.seedboxes.cc/login",
            200,
            "text/html",
            "test",
        )

    assert "<invalid-url>" in caplog.text


@pytest.mark.parametrize(
    "value",
    ["", "session_id=abc; other=value", "line\nbreak", "deleted"],
)
def test_unsafe_cookie_values_are_rejected(client_module, value):
    """Manual cookie input cannot inject another HTTP header or cookie."""
    with pytest.raises(ValueError):
        client_module.validate_session_cookie(value)


def test_login_diagnostic_redacts_before_truncation(client_module, caplog):
    """Reflected credentials and opaque tokens never reach diagnostics."""
    long_password = "SensitivePrefix" + ("x" * 220)
    client = client_module.SeedboxClient(
        FakeSession(), None, "Person@example.com", long_password
    )
    html = (
        "<title>PERSON@EXAMPLE.COM "
        f"{long_password}</title>"
        '<form action="/login-actions/authenticate/'
        'abcdefghijklmnopqrstuvwxyz123456"><input name="username"></form>'
    )

    with caplog.at_level("WARNING"):
        client._parse_and_log_login_page(
            html,
            "https://gatekeeper.seedboxes.cc/login",
            200,
            "text/html",
            "test",
        )

    diagnostic = caplog.text.lower()
    assert "person@example.com" not in diagnostic
    assert "sensitiveprefix" not in diagnostic
    assert "abcdefghijklmnopqrstuvwxyz123456" not in diagnostic

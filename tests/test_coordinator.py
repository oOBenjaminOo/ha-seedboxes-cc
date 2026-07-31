"""Tests for safe persistence of renewed Seedboxes.cc sessions."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

COMPONENT_DIR = Path(__file__).parents[1] / "custom_components" / "seedboxes_cc"


@pytest.fixture
def coordinator_module():
    """Load the coordinator with small Home Assistant interface stubs."""
    homeassistant = types.ModuleType("homeassistant")
    sys.modules["homeassistant"] = homeassistant

    config_entries = types.ModuleType("homeassistant.config_entries")

    class ConfigEntry:
        pass

    class ConfigEntryAuthFailed(Exception):
        pass

    config_entries.ConfigEntry = ConfigEntry
    config_entries.ConfigEntryAuthFailed = ConfigEntryAuthFailed
    sys.modules[config_entries.__name__] = config_entries

    const = types.ModuleType("homeassistant.const")
    const.CONF_USERNAME = "username"
    const.CONF_PASSWORD = "password"
    sys.modules[const.__name__] = const

    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    sys.modules[core.__name__] = core

    helpers = types.ModuleType("homeassistant.helpers")
    helpers.__path__ = []
    sys.modules[helpers.__name__] = helpers

    aiohttp_client = types.ModuleType("homeassistant.helpers.aiohttp_client")
    aiohttp_client.async_get_clientsession = lambda _hass: object()
    sys.modules[aiohttp_client.__name__] = aiohttp_client

    update_coordinator = types.ModuleType("homeassistant.helpers.update_coordinator")

    class DataUpdateCoordinator:
        @classmethod
        def __class_getitem__(cls, _item):
            return cls

    class UpdateFailed(Exception):
        pass

    update_coordinator.DataUpdateCoordinator = DataUpdateCoordinator
    update_coordinator.UpdateFailed = UpdateFailed
    sys.modules[update_coordinator.__name__] = update_coordinator

    package_name = "custom_components.seedboxes_cc"
    package = types.ModuleType(package_name)
    package.__path__ = [str(COMPONENT_DIR)]
    sys.modules[package_name] = package

    integration_const = types.ModuleType(f"{package_name}.const")
    integration_const.CONF_SEEDBOX_ID = "seedbox_id"
    integration_const.CONF_SESSION_COOKIE = "session_cookie"
    integration_const.DOMAIN = "seedboxes_cc"
    sys.modules[integration_const.__name__] = integration_const

    client = types.ModuleType(f"{package_name}.seedbox_client")
    client.HybridSeedboxClient = object
    client.SeedboxAuthenticationError = type(
        "SeedboxAuthenticationError", (Exception,), {}
    )
    client.SeedboxBrowserVerificationRequired = type(
        "SeedboxBrowserVerificationRequired", (Exception,), {}
    )
    client.SeedboxDataError = type("SeedboxDataError", (Exception,), {})
    sys.modules[client.__name__] = client

    module_name = f"{package_name}.coordinator"
    spec = importlib.util.spec_from_file_location(
        module_name,
        COMPONENT_DIR / "coordinator.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class FakeConfigEntries:
    """Record Home Assistant config entry updates."""

    def __init__(self) -> None:
        self.updates = []

    def async_update_entry(self, entry, *, data):
        self.updates.append(dict(data))
        entry.data = dict(data)


def make_coordinator(module, *, persisted, current, candidate):
    """Build only the coordinator state needed by the persistence helper."""
    coordinator = object.__new__(module.SeedboxDataUpdateCoordinator)
    coordinator._persisted_cookie = persisted
    coordinator._entry = types.SimpleNamespace(
        data={
            "seedbox_id": "80414",
            "username": "account",
            "password": "password",
            **({"session_cookie": current} if current is not None else {}),
        }
    )
    coordinator.api = types.SimpleNamespace(session_cookie=candidate)
    config_entries = FakeConfigEntries()
    coordinator.hass = types.SimpleNamespace(config_entries=config_entries)
    return coordinator, config_entries


def test_changed_cookie_is_persisted_without_dropping_credentials(
    coordinator_module,
):
    """A validated renewal updates only the cookie value."""
    coordinator, config_entries = make_coordinator(
        coordinator_module,
        persisted="old-cookie",
        current="old-cookie",
        candidate="new-cookie",
    )

    coordinator._async_persist_rotated_cookie()

    assert len(config_entries.updates) == 1
    assert config_entries.updates[0]["session_cookie"] == "new-cookie"
    assert config_entries.updates[0]["username"] == "account"
    assert config_entries.updates[0]["password"] == "password"


def test_credentials_only_entry_persists_first_login_cookie(coordinator_module):
    """The first automatic login converts a credentials-only entry to hybrid."""
    coordinator, config_entries = make_coordinator(
        coordinator_module,
        persisted=None,
        current=None,
        candidate="first-cookie",
    )

    coordinator._async_persist_rotated_cookie()

    assert config_entries.updates[0]["session_cookie"] == "first-cookie"


def test_concurrent_external_cookie_update_is_never_overwritten(
    coordinator_module,
):
    """Compare-and-swap protects a newer reauthentication from stale writes."""
    coordinator, config_entries = make_coordinator(
        coordinator_module,
        persisted="old-cookie",
        current="external-cookie",
        candidate="stale-cookie",
    )

    coordinator._async_persist_rotated_cookie()
    coordinator._async_persist_rotated_cookie()

    assert config_entries.updates == []

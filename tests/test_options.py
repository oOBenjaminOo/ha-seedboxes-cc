"""Tests for Seedboxes.cc options and entity metadata."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

try:
    import voluptuous as vol
except ImportError:  # pragma: no cover - allowed for dependency-light local runs
    vol = None

COMPONENT_DIR = Path(__file__).parents[1] / "custom_components" / "seedboxes_cc"


@pytest.fixture
def options_module():
    """Load the options helper without importing Home Assistant."""
    package_name = "custom_components.seedboxes_cc"
    package = types.ModuleType(package_name)
    package.__path__ = [str(COMPONENT_DIR)]
    sys.modules[package_name] = package

    const_module = types.ModuleType(f"{package_name}.const")
    const_module.CONF_SCAN_PERIOD = "scan_period"
    const_module.DEFAULT_SCAN_PERIOD = 900
    const_module.MIN_SCAN_PERIOD = 300
    sys.modules[const_module.__name__] = const_module

    module_name = f"{package_name}.options"
    spec = importlib.util.spec_from_file_location(
        module_name,
        COMPONENT_DIR / "options.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(vol is None, reason="voluptuous is not installed")
def test_options_schema_uses_default_and_coerces_seconds(options_module):
    """The form exposes only a serializable string field for the interval."""
    schema = options_module.build_options_schema({})

    assert schema({}) == {"scan_period": 900}
    assert schema({"scan_period": "600"}) == {"scan_period": 600}


@pytest.mark.skipif(vol is None, reason="voluptuous is not installed")
def test_options_schema_rejects_interval_below_minimum(options_module):
    """Intervals shorter than five minutes are rejected by the form."""
    schema = options_module.build_options_schema({})

    with pytest.raises(vol.Invalid):
        schema({"scan_period": 299})


def test_torrent_client_sensor_is_no_longer_declared():
    """The removed torrent-client entity cannot be recreated on setup."""
    sensor_source = (COMPONENT_DIR / "sensor.py").read_text(encoding="utf-8")
    strings = (COMPONENT_DIR / "strings.json").read_text(encoding="utf-8")

    assert 'key="torrent_client"' not in sensor_source
    assert '"torrent_client"' not in strings

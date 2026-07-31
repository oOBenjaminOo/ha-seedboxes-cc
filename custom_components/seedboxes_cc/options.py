"""Options helpers for the Seedboxes.cc integration."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from .const import CONF_SCAN_PERIOD, DEFAULT_SCAN_PERIOD, MIN_SCAN_PERIOD


def build_options_schema(options: Mapping[str, Any]) -> vol.Schema:
    """Build the Seedboxes.cc options schema with string field names only."""
    return vol.Schema(
        {
            vol.Required(
                CONF_SCAN_PERIOD,
                default=options.get(CONF_SCAN_PERIOD, DEFAULT_SCAN_PERIOD),
            ): vol.All(
                vol.Coerce(int),
                vol.Range(min=MIN_SCAN_PERIOD),
            )
        }
    )

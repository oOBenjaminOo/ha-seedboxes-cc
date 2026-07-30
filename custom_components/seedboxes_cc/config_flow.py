"""Config flow for the Seedboxes.cc integration."""

from __future__ import annotations

from collections import OrderedDict

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol

from .const import (
    CONF_SCAN_PERIOD,
    CONF_SEEDBOX_ID,
    CONF_SESSION_COOKIE,
    DEFAULT_SCAN_PERIOD,
    DOMAIN,
    MIN_SCAN_PERIOD,
    PLATFORMS,
)
from .seedbox_client import SeedboxClient


class SeedboxFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Seedboxes.cc."""

    VERSION = 2

    async def async_step_user(self, user_input=None):
        """Handle the initial configuration step."""
        errors = {}

        if user_input is not None:
            try:
                client = SeedboxClient(
                    async_get_clientsession(self.hass),
                    user_input[CONF_SEEDBOX_ID],
                    user_input[CONF_SESSION_COOKIE],
                )
                data = await client.async_get_data()
            except Exception:
                errors["base"] = "auth"
            else:
                await self.async_set_unique_id(str(user_input[CONF_SEEDBOX_ID]))
                self._abort_if_unique_id_configured()
                title = data.get("data", {}).get("Status")
                return self.async_create_entry(
                    title=f"Seedbox {user_input[CONF_SEEDBOX_ID]}",
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SEEDBOX_ID): str,
                    vol.Required(CONF_SESSION_COOKIE): str,
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Return the options flow."""
        return SeedboxOptionsFlowHandler(config_entry)


class SeedboxOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle Seedboxes.cc options."""

    def __init__(self, config_entry):
        self.options = dict(config_entry.options)

    async def async_step_init(self, user_input=None):
        """Manage the integration options."""
        return await self.async_step_user(user_input)

    async def async_step_user(self, user_input=None):
        """Handle options submitted by the user."""
        if user_input is not None:
            user_input[CONF_SCAN_PERIOD] = max(
                int(user_input[CONF_SCAN_PERIOD]), MIN_SCAN_PERIOD
            )
            return self.async_create_entry(title="", data=user_input)

        data_schema = OrderedDict()
        data_schema[
            vol.Optional(
                CONF_SCAN_PERIOD,
                default=self.options.get(CONF_SCAN_PERIOD, DEFAULT_SCAN_PERIOD),
            )
        ] = int

        for platform in sorted(PLATFORMS):
            data_schema[vol.Required(platform, default=self.options.get(platform, True))] = bool

        return self.async_show_form(step_id="user", data_schema=vol.Schema(data_schema))

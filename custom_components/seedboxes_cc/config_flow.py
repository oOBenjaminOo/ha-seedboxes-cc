"""Config flow for the Seedboxes.cc integration."""

from __future__ import annotations

from collections import OrderedDict
from typing import Any

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol

from .const import (
    CONF_SCAN_PERIOD,
    CONF_SEEDBOX_ID,
    DEFAULT_SCAN_PERIOD,
    DOMAIN,
    MIN_SCAN_PERIOD,
    PLATFORMS,
)
from .seedbox_client import SeedboxAuthenticationError, SeedboxClient, SeedboxDataError


class SeedboxFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Seedboxes.cc."""

    VERSION = 3

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle the initial configuration step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            seedbox_id = str(user_input[CONF_SEEDBOX_ID]).strip()
            username = str(user_input[CONF_USERNAME]).strip()
            password = str(user_input[CONF_PASSWORD])

            try:
                await self._async_validate(seedbox_id, username, password)
            except SeedboxAuthenticationError:
                errors["base"] = "invalid_auth"
            except (SeedboxDataError, TimeoutError):
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(seedbox_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Seedbox {seedbox_id}",
                    data={
                        CONF_SEEDBOX_ID: seedbox_id,
                        CONF_USERNAME: username,
                        CONF_PASSWORD: password,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SEEDBOX_ID): str,
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> config_entries.ConfigFlowResult:
        """Start reauthentication."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Validate and save replacement account credentials."""
        errors: dict[str, str] = {}

        if self._reauth_entry is None:
            return self.async_abort(reason="reauth_failed")

        seedbox_id = str(self._reauth_entry.data[CONF_SEEDBOX_ID])
        current_username = str(self._reauth_entry.data.get(CONF_USERNAME, ""))

        if user_input is not None:
            username = str(user_input[CONF_USERNAME]).strip()
            password = str(user_input[CONF_PASSWORD])

            try:
                await self._async_validate(seedbox_id, username, password)
            except SeedboxAuthenticationError:
                errors["base"] = "invalid_auth"
            except (SeedboxDataError, TimeoutError):
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                errors["base"] = "unknown"
            else:
                new_data = {
                    **self._reauth_entry.data,
                    CONF_USERNAME: username,
                    CONF_PASSWORD: password,
                }
                new_data.pop("session_cookie", None)
                self.hass.config_entries.async_update_entry(
                    self._reauth_entry,
                    data=new_data,
                    version=self.VERSION,
                )
                await self.hass.config_entries.async_reload(
                    self._reauth_entry.entry_id
                )
                return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME, default=current_username): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
            description_placeholders={"seedbox_id": seedbox_id},
        )

    async def _async_validate(
        self, seedbox_id: str, username: str, password: str
    ) -> None:
        """Validate account credentials and seedbox access."""
        client = SeedboxClient(
            async_get_clientsession(self.hass),
            seedbox_id,
            username,
            password,
        )
        await client.async_validate_credentials()

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

        for platform in sorted(PLATFORMS, key=str):
            data_schema[
                vol.Required(platform, default=self.options.get(platform, True))
            ] = bool

        return self.async_show_form(step_id="user", data_schema=vol.Schema(data_schema))

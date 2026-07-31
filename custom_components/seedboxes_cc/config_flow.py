"""Config flow for the Seedboxes.cc integration."""

from __future__ import annotations

from collections import OrderedDict
import logging
from typing import Any

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
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
from .seedbox_client import (
    SeedboxAuthenticationError,
    SeedboxBrowserVerificationRequired,
    SeedboxClient,
    SeedboxDataError,
    SessionCookieSeedboxClient,
)

_LOGGER = logging.getLogger(__name__)


class SeedboxFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Seedboxes.cc."""

    VERSION = 5

    def __init__(self) -> None:
        self._username: str | None = None
        self._password: str | None = None
        self._discovered: dict[str, dict[str, Any]] = {}
        self._reauth_entry = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Authenticate and discover the account seedboxes."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._username = str(user_input[CONF_USERNAME]).strip()
            self._password = str(user_input[CONF_PASSWORD])
            try:
                client = SeedboxClient(
                    async_get_clientsession(self.hass),
                    None,
                    self._username,
                    self._password,
                )
                self._discovered = await client.async_discover_seedboxes()
            except SeedboxBrowserVerificationRequired:
                _LOGGER.info(
                    "Seedboxes.cc requires browser verification; "
                    "switching to session cookie authentication"
                )
                return await self.async_step_session_cookie()
            except SeedboxAuthenticationError as err:
                _LOGGER.warning(
                    "Seedboxes.cc authentication failed during automatic discovery: %s",
                    err,
                )
                errors["base"] = "invalid_auth"
            except (SeedboxDataError, TimeoutError) as err:
                _LOGGER.error(
                    "Seedboxes.cc automatic discovery failed: %s",
                    err,
                    exc_info=True,
                )
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception(
                    "Unexpected error while authenticating with Seedboxes.cc and discovering seedboxes"
                )
                errors["base"] = "unknown"
            else:
                _LOGGER.debug(
                    "Seedboxes.cc automatic discovery found %d seedbox(es)",
                    len(self._discovered),
                )
                if len(self._discovered) == 1:
                    seedbox_id = next(iter(self._discovered))
                    return await self._async_create_seedbox_entry(seedbox_id)
                return await self.async_step_select_seedbox()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    async def async_step_session_cookie(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Configure access with a browser session cookie."""
        errors: dict[str, str] = {}

        if user_input is not None:
            seedbox_id = str(user_input[CONF_SEEDBOX_ID]).strip()
            session_cookie = str(user_input[CONF_SESSION_COOKIE]).strip()
            try:
                client = SessionCookieSeedboxClient(
                    async_get_clientsession(self.hass),
                    seedbox_id,
                    session_cookie,
                )
                await client.async_validate_credentials()
            except SeedboxAuthenticationError:
                errors["base"] = "invalid_session"
            except (SeedboxDataError, TimeoutError) as err:
                _LOGGER.error(
                    "Seedboxes.cc session validation failed: %s",
                    err,
                    exc_info=True,
                )
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception(
                    "Unexpected error while validating Seedboxes.cc session"
                )
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(seedbox_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Seedbox {seedbox_id}",
                    data={
                        CONF_SEEDBOX_ID: seedbox_id,
                        CONF_SESSION_COOKIE: session_cookie,
                    },
                )

        return self.async_show_form(
            step_id="session_cookie",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SEEDBOX_ID): str,
                    vol.Required(CONF_SESSION_COOKIE): str,
                }
            ),
            errors=errors,
        )

    async def async_step_select_seedbox(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Let the user select one seedbox when several are available."""
        if not self._discovered or self._username is None or self._password is None:
            return self.async_abort(reason="discovery_failed")

        if user_input is not None:
            return await self._async_create_seedbox_entry(
                str(user_input[CONF_SEEDBOX_ID])
            )

        choices: dict[str, str] = {}
        for seedbox_id, details in self._discovered.items():
            server = details.get("server") or {}
            package = details.get("package") or {}
            username = details.get("username") or f"Seedbox {seedbox_id}"
            label_parts = [str(username)]
            if server.get("name"):
                label_parts.append(str(server["name"]))
            if package.get("name"):
                label_parts.append(str(package["name"]))
            choices[seedbox_id] = " — ".join(label_parts)

        return self.async_show_form(
            step_id="select_seedbox",
            data_schema=vol.Schema(
                {vol.Required(CONF_SEEDBOX_ID): vol.In(choices)}
            ),
        )

    async def _async_create_seedbox_entry(
        self, seedbox_id: str
    ) -> config_entries.ConfigFlowResult:
        """Create an entry for the selected seedbox."""
        await self.async_set_unique_id(seedbox_id)
        self._abort_if_unique_id_configured()
        details = self._discovered.get(seedbox_id, {})
        server = details.get("server") or {}
        title = details.get("username") or server.get("name") or f"Seedbox {seedbox_id}"
        return self.async_create_entry(
            title=str(title),
            data={
                CONF_SEEDBOX_ID: seedbox_id,
                CONF_USERNAME: self._username,
                CONF_PASSWORD: self._password,
            },
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> config_entries.ConfigFlowResult:
        """Start reauthentication."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        if CONF_SESSION_COOKIE in entry_data:
            return await self.async_step_reauth_session_cookie()
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
                client = SeedboxClient(
                    async_get_clientsession(self.hass),
                    seedbox_id,
                    username,
                    password,
                )
                await client.async_validate_credentials()
            except SeedboxBrowserVerificationRequired:
                return await self.async_step_reauth_session_cookie()
            except SeedboxAuthenticationError as err:
                _LOGGER.warning(
                    "Seedboxes.cc reauthentication failed for seedbox %s: %s",
                    seedbox_id,
                    err,
                )
                errors["base"] = "invalid_auth"
            except (SeedboxDataError, TimeoutError) as err:
                _LOGGER.error(
                    "Seedboxes.cc reauthentication could not validate seedbox %s: %s",
                    seedbox_id,
                    err,
                    exc_info=True,
                )
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception(
                    "Unexpected error while reauthenticating Seedboxes.cc seedbox %s",
                    seedbox_id,
                )
                errors["base"] = "unknown"
            else:
                self.hass.config_entries.async_update_entry(
                    self._reauth_entry,
                    data={
                        **self._reauth_entry.data,
                        CONF_USERNAME: username,
                        CONF_PASSWORD: password,
                    },
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

    async def async_step_reauth_session_cookie(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Replace blocked credentials with a browser session cookie."""
        errors: dict[str, str] = {}
        if self._reauth_entry is None:
            return self.async_abort(reason="reauth_failed")

        seedbox_id = str(self._reauth_entry.data[CONF_SEEDBOX_ID])
        if user_input is not None:
            session_cookie = str(user_input[CONF_SESSION_COOKIE]).strip()
            try:
                client = SessionCookieSeedboxClient(
                    async_get_clientsession(self.hass),
                    seedbox_id,
                    session_cookie,
                )
                await client.async_validate_credentials()
            except SeedboxAuthenticationError:
                errors["base"] = "invalid_session"
            except (SeedboxDataError, TimeoutError):
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception(
                    "Unexpected error while validating replacement "
                    "Seedboxes.cc session"
                )
                errors["base"] = "unknown"
            else:
                self.hass.config_entries.async_update_entry(
                    self._reauth_entry,
                    data={
                        CONF_SEEDBOX_ID: seedbox_id,
                        CONF_SESSION_COOKIE: session_cookie,
                    },
                    version=self.VERSION,
                )
                await self.hass.config_entries.async_reload(
                    self._reauth_entry.entry_id
                )
                return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_session_cookie",
            data_schema=vol.Schema(
                {vol.Required(CONF_SESSION_COOKIE): str}
            ),
            errors=errors,
            description_placeholders={"seedbox_id": seedbox_id},
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
        for platform in sorted(PLATFORMS, key=str):
            data_schema[
                vol.Required(platform, default=self.options.get(platform, True))
            ] = bool
        return self.async_show_form(step_id="user", data_schema=vol.Schema(data_schema))

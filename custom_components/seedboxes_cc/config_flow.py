"""Config flow for the Seedboxes.cc integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_SEEDBOX_ID,
    CONF_SESSION_COOKIE,
    CONFIG_ENTRY_MINOR_VERSION,
    CONFIG_ENTRY_VERSION,
    DOMAIN,
)
from .options import build_options_schema
from .seedbox_client import (
    SeedboxAuthenticationError,
    SeedboxBrowserVerificationRequired,
    SeedboxClient,
    SeedboxDataError,
    SeedboxDiscoveryError,
    SessionCookieSeedboxClient,
    validate_seedbox_id,
    validate_session_cookie,
)

_LOGGER = logging.getLogger(__name__)

_USERNAME_SELECTOR = TextSelector(TextSelectorConfig(autocomplete="username"))
_PASSWORD_SELECTOR = TextSelector(
    TextSelectorConfig(
        type=TextSelectorType.PASSWORD,
        autocomplete="current-password",
    )
)
_SESSION_COOKIE_SELECTOR = TextSelector(
    TextSelectorConfig(
        type=TextSelectorType.PASSWORD,
        autocomplete="off",
    )
)


class SeedboxFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Seedboxes.cc."""

    VERSION = CONFIG_ENTRY_VERSION
    MINOR_VERSION = CONFIG_ENTRY_MINOR_VERSION

    def __init__(self) -> None:
        self._username: str | None = None
        self._password: str | None = None
        self._session_cookie: str | None = None
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
                self._session_cookie = client.session_cookie
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
            except (SeedboxDataError, TimeoutError):
                _LOGGER.exception("Seedboxes.cc automatic discovery failed")
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
                    vol.Required(CONF_USERNAME): _USERNAME_SELECTOR,
                    vol.Required(CONF_PASSWORD): _PASSWORD_SELECTOR,
                }
            ),
            errors=errors,
        )

    async def async_step_session_cookie(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Discover seedboxes with a browser session cookie."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                self._session_cookie = validate_session_cookie(
                    str(user_input[CONF_SESSION_COOKIE])
                )
                client = SessionCookieSeedboxClient(
                    async_get_clientsession(self.hass),
                    None,
                    self._session_cookie,
                )
                self._discovered = await client.async_discover_seedboxes()
                self._session_cookie = client.session_cookie
            except (SeedboxAuthenticationError, ValueError):
                errors["base"] = "invalid_session"
            except SeedboxDiscoveryError as err:
                _LOGGER.warning(
                    "Seedboxes.cc could not discover a seedbox from the session: %s",
                    err,
                )
                return await self.async_step_manual_seedbox()
            except (SeedboxDataError, TimeoutError):
                _LOGGER.exception("Seedboxes.cc session validation or discovery failed")
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception(
                    "Unexpected error while validating Seedboxes.cc session"
                )
                errors["base"] = "unknown"
            else:
                if len(self._discovered) == 1:
                    return await self._async_create_seedbox_entry(
                        next(iter(self._discovered))
                    )
                return await self.async_step_select_seedbox()

        return self.async_show_form(
            step_id="session_cookie",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SESSION_COOKIE): _SESSION_COOKIE_SELECTOR,
                }
            ),
            errors=errors,
        )

    async def async_step_manual_seedbox(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Validate a manually supplied seedbox ID as a discovery fallback."""
        errors: dict[str, str] = {}
        if self._session_cookie is None:
            return self.async_abort(reason="discovery_failed")

        if user_input is not None:
            try:
                seedbox_id = validate_seedbox_id(str(user_input[CONF_SEEDBOX_ID]))
                client = SessionCookieSeedboxClient(
                    async_get_clientsession(self.hass),
                    seedbox_id,
                    self._session_cookie,
                )
                await client.async_validate_credentials()
                self._session_cookie = client.session_cookie
            except ValueError:
                errors["base"] = "invalid_seedbox_id"
            except SeedboxAuthenticationError:
                errors["base"] = "invalid_session"
            except (SeedboxDataError, TimeoutError):
                _LOGGER.exception("Seedboxes.cc manual seedbox validation failed")
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception(
                    "Unexpected error while validating a Seedboxes.cc seedbox"
                )
                errors["base"] = "unknown"
            else:
                self._discovered = {seedbox_id: {}}
                return await self._async_create_seedbox_entry(seedbox_id)

        return self.async_show_form(
            step_id="manual_seedbox",
            data_schema=vol.Schema({vol.Required(CONF_SEEDBOX_ID): str}),
            errors=errors,
        )

    async def async_step_select_seedbox(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Let the user select one seedbox when several are available."""
        if not self._discovered:
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
            data_schema=vol.Schema({vol.Required(CONF_SEEDBOX_ID): vol.In(choices)}),
        )

    async def _async_create_seedbox_entry(
        self, seedbox_id: str
    ) -> config_entries.ConfigFlowResult:
        """Create an entry for the selected seedbox."""
        seedbox_id = validate_seedbox_id(seedbox_id)
        await self.async_set_unique_id(seedbox_id)
        self._abort_if_unique_id_configured()
        details = self._discovered.get(seedbox_id, {})
        server = details.get("server") or {}
        title = details.get("username") or server.get("name") or f"Seedbox {seedbox_id}"
        data: dict[str, Any] = {CONF_SEEDBOX_ID: seedbox_id}
        if self._username is not None:
            data[CONF_USERNAME] = self._username
        if self._password is not None:
            data[CONF_PASSWORD] = self._password
        if self._session_cookie is not None:
            data[CONF_SESSION_COOKIE] = self._session_cookie
        return self.async_create_entry(
            title=str(title),
            data=data,
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> config_entries.ConfigFlowResult:
        """Start reauthentication."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        if self._reauth_entry is None:
            return self.async_abort(reason="reauth_failed")
        self._username = self._reauth_entry.data.get(CONF_USERNAME)
        self._password = self._reauth_entry.data.get(CONF_PASSWORD)
        self._session_cookie = self._reauth_entry.data.get(CONF_SESSION_COOKIE)
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
            self._username = str(user_input[CONF_USERNAME]).strip()
            self._password = str(user_input[CONF_PASSWORD])
            try:
                client = SeedboxClient(
                    async_get_clientsession(self.hass),
                    seedbox_id,
                    self._username,
                    self._password,
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
            except (SeedboxDataError, TimeoutError):
                _LOGGER.exception(
                    "Seedboxes.cc reauthentication could not validate seedbox %s",
                    seedbox_id,
                )
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception(
                    "Unexpected error while reauthenticating Seedboxes.cc seedbox %s",
                    seedbox_id,
                )
                errors["base"] = "unknown"
            else:
                self._session_cookie = client.session_cookie
                return self.async_update_reload_and_abort(
                    self._reauth_entry,
                    data_updates={
                        CONF_USERNAME: self._username,
                        CONF_PASSWORD: self._password,
                        CONF_SESSION_COOKIE: self._session_cookie,
                    },
                    reload_even_if_entry_is_unchanged=False,
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_USERNAME, default=current_username
                    ): _USERNAME_SELECTOR,
                    vol.Required(CONF_PASSWORD): _PASSWORD_SELECTOR,
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
            try:
                self._session_cookie = validate_session_cookie(
                    str(user_input[CONF_SESSION_COOKIE])
                )
                client = SessionCookieSeedboxClient(
                    async_get_clientsession(self.hass),
                    seedbox_id,
                    self._session_cookie,
                )
                await client.async_validate_credentials()
                self._session_cookie = client.session_cookie
            except (SeedboxAuthenticationError, ValueError):
                errors["base"] = "invalid_session"
            except (SeedboxDataError, TimeoutError):
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception(
                    "Unexpected error while validating replacement Seedboxes.cc session"
                )
                errors["base"] = "unknown"
            else:
                updates: dict[str, Any] = {
                    CONF_SESSION_COOKIE: self._session_cookie,
                }
                if self._username is not None:
                    updates[CONF_USERNAME] = self._username
                if self._password is not None:
                    updates[CONF_PASSWORD] = self._password
                return self.async_update_reload_and_abort(
                    self._reauth_entry,
                    data_updates=updates,
                    reload_even_if_entry_is_unchanged=False,
                )

        return self.async_show_form(
            step_id="reauth_session_cookie",
            data_schema=vol.Schema(
                {vol.Required(CONF_SESSION_COOKIE): _SESSION_COOKIE_SELECTOR}
            ),
            errors=errors,
            description_placeholders={"seedbox_id": seedbox_id},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Return the options flow."""
        return SeedboxOptionsFlowHandler()


class SeedboxOptionsFlowHandler(config_entries.OptionsFlowWithReload):
    """Handle Seedboxes.cc options."""

    async def async_step_init(self, user_input=None):
        """Manage the integration options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=build_options_schema(self.config_entry.options),
        )

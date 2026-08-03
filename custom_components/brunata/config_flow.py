"""Config flow for Brunata integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from brunata_api import Client

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.core import callback

from .const import DOMAIN, CONF_EMAIL, CONF_PASSWORD, CONF_DEBUG_LOGGING

_LOGGER = logging.getLogger(__name__)

async def validate_input(hass: HomeAssistant, data: dict[str, str]) -> dict[str, str]:
    """Validate the user input allows us to connect.

    Data has the keys from STEP_USER_DATA_SCHEMA with values provided by the user.
    """
    _LOGGER.debug("Validating input for %s", data[CONF_EMAIL])
    client = await hass.async_add_executor_job(Client, data[CONF_EMAIL], data[CONF_PASSWORD])

    try:
        # Attempt to fetch meters to validate login
        _LOGGER.debug("Attempting to validate login by fetching meters for %s", data[CONF_EMAIL])
        # The library has a bug with await on dict in _renew_tokens/_b2c_auth
        try:
            meters = await client.get_meters()
        except TypeError as err:
            if "await" in str(err) and "dict" in str(err):
                _LOGGER.error("Error in brunata-api library: 'object dict can't be used in await expression'")
            raise InvalidAuth from err
        except UnboundLocalError as err:
            # brunata_api bug: when the network is unavailable, api_wrapper raises
            # ConnectError which the library catches internally, but then continues
            # and tries to use the 'response' variable that was never assigned.
            # This surfaces as an UnboundLocalError instead of a connection error.
            if "'response'" in str(err):
                _LOGGER.error("Cannot connect to Brunata API (network error): %s", err)
                raise CannotConnect from err
            raise InvalidAuth from err

        if isinstance(meters, dict) and (
            meters.get("errorCode") is not None
            or meters.get("errorMessage") is not None
        ):
            _LOGGER.error("Brunata API returned error response during login validation: %s", meters)
            raise InvalidAuth

        if meters:
            _LOGGER.debug("Login validated, found %s meters", len(meters))
        else:
            _LOGGER.warning("Login validated, but no meters found")
    except (InvalidAuth, CannotConnect):
        raise
    except Exception as err:
        _LOGGER.error("Could not validate Brunata login: %s", err)
        raise InvalidAuth from err

    return {"title": data[CONF_EMAIL]}

class BrunataConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Brunata."""

    VERSION = 1

    async def async_step_user(self, user_input=None) -> FlowResult:
        """Handle the initial step."""
        _LOGGER.debug("async_step_user called with input: %s", user_input)
        errors = {}
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_EMAIL])
            self._abort_if_unique_id_configured()
            try:
                info = await validate_input(self.hass, user_input)
                _LOGGER.debug("Config entry created for %s", user_input[CONF_EMAIL])
                return self.async_create_entry(title=info["title"], data=user_input)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected error during config flow")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(self, user_input=None) -> FlowResult:
        """Handle re-authentication when credentials are no longer valid."""
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        assert entry is not None
        errors = {}

        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input)
                self.hass.config_entries.async_update_entry(entry, data=user_input)
                _LOGGER.debug("Re-authentication successful for %s", user_input[CONF_EMAIL])
                return self.async_abort(reason="reauth_successful")
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected error during re-authentication")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL, default=entry.data.get(CONF_EMAIL)): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> BrunataOptionsFlowHandler:
        """Get the options flow for this handler."""
        return BrunataOptionsFlowHandler()

class BrunataOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle Brunata options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the Brunata options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_DEBUG_LOGGING,
                        default=self.config_entry.options.get(CONF_DEBUG_LOGGING, False),
                    ): bool,
                }
            ),
        )

class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect to the Brunata API."""

class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""

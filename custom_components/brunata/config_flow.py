"""Config flow for Brunata integration."""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult, OptionsFlowWithReload
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import selector

from .api import (
    BrunataApiClient,
    BrunataApiError,
    BrunataAuthError,
    BrunataConnectionError,
)
from .const import DOMAIN, CONF_EMAIL, CONF_PASSWORD, CONF_DEBUG_LOGGING

_LOGGER = logging.getLogger(__name__)


def _normalise_email(email: str) -> str:
    """Return the email in the canonical form used for the entry's unique_id."""
    return email.strip().lower()


# A bare `str` in the schema renders as an ordinary text box, so the password
# was shown in cleartext while being typed and password managers had nothing to
# latch onto. TextSelector gives the frontend the field type it needs: a masked
# input for the password and an email keyboard on mobile for the address.
EMAIL_SELECTOR = selector.TextSelector(
    selector.TextSelectorConfig(
        type=selector.TextSelectorType.EMAIL,
        autocomplete="username",
    )
)
PASSWORD_SELECTOR = selector.TextSelector(
    selector.TextSelectorConfig(
        type=selector.TextSelectorType.PASSWORD,
        autocomplete="current-password",
    )
)


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the credentials by performing a real login.

    Errors are mapped from the API layer's own exception types, so a network
    problem is reported as one instead of being mistaken for a bad password —
    which previously sent people off changing credentials that were fine.
    """
    client = BrunataApiClient(hass, data[CONF_EMAIL], data[CONF_PASSWORD])
    try:
        await client.async_validate_credentials()
    except BrunataAuthError as err:
        raise InvalidAuth from err
    except BrunataConnectionError as err:
        raise CannotConnect from err
    except BrunataApiError as err:
        _LOGGER.error("Brunata rejected the login attempt: %s", err)
        raise CannotConnect from err
    finally:
        # The client owns an httpx session; without this every login attempt,
        # successful or not, leaks one.
        await client.async_close()

    return {"title": f"Brunata ({data[CONF_EMAIL]})"}


class BrunataConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Brunata."""

    VERSION = 1

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        """Handle the initial step."""
        # Never log user_input itself: it contains the password, and debug logs
        # are routinely attached to bug reports.
        _LOGGER.debug("async_step_user called (form submitted: %s)", user_input is not None)
        errors = {}
        if user_input is not None:
            # Normalise before using it as the unique_id, otherwise
            # "Bruger@example.com" and "bruger@example.com" are treated as two
            # separate accounts and the duplicate check never fires.
            await self.async_set_unique_id(_normalise_email(user_input[CONF_EMAIL]))
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
                    vol.Required(CONF_EMAIL): EMAIL_SELECTOR,
                    vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Handle re-authentication when credentials are no longer valid."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None) -> ConfigFlowResult:
        """Confirm re-authentication dialog."""
        reauth_entry = self._get_reauth_entry()
        errors = {}

        if user_input is not None:
            try:
                await validate_input(self.hass, user_input)
                # Same normalisation as in async_step_user, so re-entering the
                # correct address with different casing is not rejected as a
                # different account.
                await self.async_set_unique_id(_normalise_email(user_input[CONF_EMAIL]))
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                _LOGGER.debug("Re-authentication successful for %s", user_input[CONF_EMAIL])
                return self.async_update_reload_and_abort(
                    reauth_entry,
                    data_updates=user_input,
                )
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except AbortFlow:
                # Raised internally by _abort_if_unique_id_mismatch(); must
                # propagate so the flow manager turns it into a proper
                # FlowResultType.ABORT instead of being swallowed here.
                raise
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected error during re-authentication")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_EMAIL, default=reauth_entry.data.get(CONF_EMAIL)
                    ): EMAIL_SELECTOR,
                    vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
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

class BrunataOptionsFlowHandler(OptionsFlowWithReload):
    """Handle Brunata options.

    Subclassing OptionsFlowWithReload (instead of plain OptionsFlow) makes
    Home Assistant automatically reload the config entry after the options
    form is saved — the recommended replacement for a manual
    entry.add_update_listener() whose only job is to trigger a reload. See
    async_setup_entry() in __init__.py for the corresponding note.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
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

"""Config flow for Brunata integration."""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import selector

from .api import (
    BrunataApiClient,
    BrunataApiError,
    BrunataAuthError,
    BrunataConnectionError,
)
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def _normalise_email(email: str) -> str:
    """Return the email in the canonical form the integration stores it in.

    Applied to the whole of user_input before anything is done with it, not
    just to the unique_id. Storing the raw string meant a value pasted from a
    password manager as "  bruger@example.com " was sent to Keycloak with the
    whitespace attached: the login failed, the user saw invalid_auth, and
    nothing in the UI hinted at why. It also put the padding in the entry
    title, and offered it back as the default on the reauth form.

    Lowercasing as well as stripping keeps the stored address and the
    unique_id derived from it in one form, so the two can never disagree.
    """
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
    client = await BrunataApiClient.async_create(
        hass, data[CONF_EMAIL], data[CONF_PASSWORD]
    )
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

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        # Never log user_input itself: it contains the password, and debug logs
        # are routinely attached to bug reports. The email address is left out
        # of the success lines below for the same reason — it identifies the
        # account to anyone reading the report, and entry_id does the job.
        _LOGGER.debug("async_step_user called (form submitted: %s)", user_input is not None)
        errors = {}
        if user_input is not None:
            # Normalised once, here, so the unique_id, the credentials sent to
            # Brunata and the data written to the entry are all the same
            # string. Doing it only for the unique_id left the raw value —
            # whitespace and all — to be logged in with and stored.
            user_input = {
                **user_input,
                CONF_EMAIL: _normalise_email(user_input[CONF_EMAIL]),
            }
            # Without this, "Bruger@example.com" and "bruger@example.com" are
            # treated as two separate accounts and the duplicate check never
            # fires.
            await self.async_set_unique_id(user_input[CONF_EMAIL])
            self._abort_if_unique_id_configured()
            try:
                info = await validate_input(self.hass, user_input)
                _LOGGER.debug("Config entry created")
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

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm re-authentication dialog."""
        reauth_entry = self._get_reauth_entry()
        errors = {}

        if user_input is not None:
            # Normalised before the login for the same reason as in
            # async_step_user: a padded address fails against Keycloak and the
            # user is told their password is wrong.
            user_input = {
                **user_input,
                CONF_EMAIL: _normalise_email(user_input[CONF_EMAIL]),
            }
            try:
                await validate_input(self.hass, user_input)
                # Re-entering the correct address with different casing must
                # not be rejected as a different account.
                await self.async_set_unique_id(user_input[CONF_EMAIL])
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                _LOGGER.debug(
                    "Re-authentication successful for entry %s", reauth_entry.entry_id
                )
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


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect to the Brunata API."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""

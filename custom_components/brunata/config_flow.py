"""Config flow for Brunata integration."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, selector

from .api import (
    BrunataApiClient,
    BrunataApiError,
    BrunataAuthError,
    BrunataConnectionError,
)
from .const import DEVICE_ID_PREFIX, DOMAIN

_LOGGER = logging.getLogger(__name__)


def _normalise_email(email: str) -> str:
    """Return the email in the canonical form the integration stores it in.

    Applied to the whole of user_input, not just the unique_id. Storing the raw
    string meant a value pasted from a password manager as
    "  bruger@example.com " was sent to Keycloak with the whitespace attached:
    the login failed, the user saw invalid_auth, and nothing in the UI hinted
    at why. It also put the padding in the entry title and offered it back as
    the reauth form's default.

    Lowercasing as well as stripping keeps the stored address and the unique_id
    derived from it in one form, so the two can never disagree.
    """
    return email.strip().lower()


def _entry_title(email: str) -> str:
    """Return the title shown for an account in the integrations list.

    One definition, because two steps write it: async_step_user when the entry
    is created, and async_step_reconfigure when the address moves to another
    one. Two literals would let the same integration end up with two shapes of
    title depending on which step last touched it.
    """
    return f"Brunata ({email})"


# A bare `str` renders as an ordinary text box, so the password was shown in
# cleartext while being typed and password managers had nothing to latch onto.
# TextSelector gives the frontend the field type it needs: a masked input for
# the password, an email keyboard on mobile for the address.
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

    return {"title": _entry_title(data[CONF_EMAIL])}


def _meter_ids_with_a_device(hass: HomeAssistant, entry: ConfigEntry) -> set[str]:
    """Return the meter ids this entry already has a device for.

    Read from the device registry rather than from the coordinator, because
    the reconfigure dialog can be opened while the entry is not loaded, and
    then there is no coordinator to ask. Every meter device carries the
    identifier DEVICE_ID_PREFIX followed by the meter id, the registry holds it
    whether or not the integration is running, and those devices are exactly
    what moving the entry to another account would leave behind.
    """
    registry = dr.async_get(hass)
    return {
        identifier.removeprefix(DEVICE_ID_PREFIX)
        for device in dr.async_entries_for_config_entry(registry, entry.entry_id)
        for domain, identifier in device.identifiers
        if domain == DOMAIN and identifier.startswith(DEVICE_ID_PREFIX)
    }


async def _async_missing_meters(
    hass: HomeAssistant, entry: ConfigEntry, data: dict[str, Any]
) -> frozenset[str]:
    """Log in with `data` and return the entry's meters that account lacks.

    An empty result means every meter this entry already has a device for was
    found again, which is the test for "the same household under a new
    address". Anything else names the meters that would be orphaned.

    The meter lookup logs in on the way, and it raises the same two exceptions
    validate_input() does, so a caller needs one of the two and not both.

    A meter Brunata skipped this poll because its unit code did not resolve
    counts as reported. It is still on the wall — api.py drops it from the
    parsed result only so it cannot become an entity carrying a raw code as its
    unit — and treating it as gone would block a move that was legitimate.
    """
    # The login happens even when the entry has no devices at all, and its
    # result is what the caller relies on to know the credentials are good. A
    # shortcut here would let a mistyped password be written to the entry
    # unchecked, and the next poll would open the re-authentication dialog.
    known = _meter_ids_with_a_device(hass, entry)

    client = await BrunataApiClient.async_create(
        hass, data[CONF_EMAIL], data[CONF_PASSWORD]
    )
    try:
        meters = await client.async_get_meters()
        reported = set(meters) | set(
            client.last_parse_report().unresolved_unit_meter_ids
        )
    except BrunataAuthError as err:
        raise InvalidAuth from err
    except BrunataConnectionError as err:
        raise CannotConnect from err
    except BrunataApiError as err:
        _LOGGER.error("Brunata rejected the meter lookup: %s", err)
        raise CannotConnect from err
    finally:
        await client.async_close()

    return frozenset(known - reported)


class BrunataConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Brunata."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        # Never log user_input: it contains the password, and debug logs get
        # attached to bug reports. The email is left out of the success lines
        # for the same reason — it identifies the account, and entry_id does
        # the job.
        _LOGGER.debug("async_step_user called (form submitted: %s)", user_input is not None)
        errors = {}
        if user_input is not None:
            # Normalised once, here, so the unique_id, the credentials sent to
            # Brunata and the data written to the entry are the same string.
            user_input = {
                **user_input,
                CONF_EMAIL: _normalise_email(user_input[CONF_EMAIL]),
            }
            # Without this, "Bruger@example.com" and "bruger@example.com" are
            # two separate accounts and the duplicate check never fires.
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
        """Confirm re-authentication dialog.

        The address is fixed here even though async_step_reconfigure lets it
        move. Reauth starts on its own, from a failing poll, so the account it
        is repairing is the one the user was already using; an address typed
        into a dialog that appeared unasked is a typo far more often than it is
        a deliberate move. Moving to a new account is a decision, and it is
        made in the reconfigure dialog, which the user opens themselves.
        """
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
            # The account check first, the login second. Both orders end in the
            # same wrong_account abort, but this one decides it locally: a full
            # Keycloak round trip against a bot-protected endpoint to reach an
            # answer the unique_id already knows costs the user seconds and
            # Brunata a request for nothing.
            #
            # It also puts _abort_if_unique_id_mismatch()'s AbortFlow outside
            # the try below, where it belongs: it has to reach the flow manager
            # to become a proper FlowResultType.ABORT, and inside the try it
            # needed an explicit re-raise to get past the broad handler.
            await self.async_set_unique_id(user_input[CONF_EMAIL])
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            try:
                await validate_input(self.hass, user_input)
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

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user replace either stored credential before anything fails.

        The password, so that one changed in Brunata Online can be handed over
        without waiting for the integration to fail on the old one. Polling is
        hourly, so that is up to an hour of a broken integration and a "could
        not authenticate" notification for something already fixed.

        The address, so that a Brunata account reached under a new e-mail is
        not a reason to delete the integration and set it up again, which
        starts every meter's long term statistics from zero. Moving it is safe:
        entry_id is frozen and it is entry_id the entity and device registries
        are keyed on, while unique_id is a field on the config entry that
        neither registry reads. The sensors' own identifiers are built from the
        meter id, not from the address.

        A new address is accepted only if the account behind it reports every
        meter this entry already has a device for. See _async_missing_meters()
        for what a partial match would cost.
        """
        entry = self._get_reconfigure_entry()
        # Normalised on the way out of the entry, not assumed to have been
        # normalised on the way in. async_step_user only started doing that in
        # 1.4.0; an older entry can hold "  Bruger@Example.COM " verbatim,
        # because only the unique_id derived from it was cleaned up. Offering
        # that string back as the form's default would send it to Keycloak
        # again, which is the exact failure normalisation was added to prevent.
        current_email = _normalise_email(entry.data[CONF_EMAIL])
        errors = {}

        if user_input is not None:
            email = _normalise_email(user_input[CONF_EMAIL])
            credentials = {
                CONF_EMAIL: email,
                CONF_PASSWORD: user_input[CONF_PASSWORD],
            }
            moving = email != current_email

            if moving and self._address_belongs_to_another_entry(entry, email):
                # Checked before anything is written, because afterwards there
                # is no way back. Home Assistant accepts a unique_id another
                # entry already holds: it logs "Unique id of config entry ...
                # changed to ... which is already in use" and leaves two
                # entries claiming the same account, a state no code here can
                # undo. Only reachable with two Brunata integrations set up.
                return self.async_abort(reason="account_already_configured")

            try:
                # One login either way. When the address moves, the meter
                # lookup proves the credentials as it goes, so validate_input()
                # would only add a second round trip against a bot-protected
                # endpoint.
                if moving:
                    missing = await _async_missing_meters(
                        self.hass, entry, credentials
                    )
                else:
                    await validate_input(self.hass, credentials)
                    missing = frozenset()
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected error during reconfiguration")
                errors["base"] = "unknown"
            else:
                if missing:
                    # Logged because this has never been observed, and the log
                    # line is the only thing that would say what Brunata did
                    # with the meter numbers when it happens. Meter ids, not
                    # the address: the address identifies the account.
                    _LOGGER.warning(
                        "Refusing to move entry %s to another address: the new "
                        "account does not report meter(s) %s, which would be "
                        "left without data",
                        entry.entry_id,
                        ", ".join(sorted(missing)),
                    )
                    return self.async_abort(reason="meter_mismatch")

                if moving:
                    # unique_id and title are written here rather than through
                    # async_update_reload_and_abort(), which takes data updates
                    # only. The call below then updates the data and reloads.
                    self.hass.config_entries.async_update_entry(
                        entry, unique_id=email, title=_entry_title(email)
                    )
                _LOGGER.debug(
                    "Reconfiguration successful for entry %s", entry.entry_id
                )
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates=credentials,
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL, default=current_email): EMAIL_SELECTOR,
                    vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
                }
            ),
            errors=errors,
        )

    def _address_belongs_to_another_entry(
        self, entry: ConfigEntry, email: str
    ) -> bool:
        """Return whether some other Brunata entry already holds this address.

        Written out rather than delegated to _abort_if_unique_id_configured()
        or _abort_if_unique_id_mismatch(). Both are built around the entry the
        flow belongs to, and which of them treats the entry being reconfigured
        as an exception is a detail of the Home Assistant version underneath —
        one that would have to be re-checked on every floor change. Six lines
        of our own answer the question the same way on every version.
        """
        return any(
            other.entry_id != entry.entry_id and other.unique_id == email
            for other in self.hass.config_entries.async_entries(DOMAIN)
        )


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect to the Brunata API."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""

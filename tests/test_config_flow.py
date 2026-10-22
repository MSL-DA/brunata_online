"""Test Brunata config flow."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol

from homeassistant import config_entries, data_entry_flow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, selector
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.brunata.api import (
    BrunataApiError,
    BrunataAuthError,
    BrunataConnectionError,
    ParseReport,
)
from custom_components.brunata.config_flow import (
    CannotConnect,
    InvalidAuth,
    validate_input,
)
from custom_components.brunata.const import DEVICE_ID_PREFIX, DOMAIN

CREDENTIALS = {"email": "test@example.com", "password": "password123"}


def _add_meter_device(hass: HomeAssistant, entry: MockConfigEntry, meter_id: str):
    """Register the device a meter would have, without setting the entry up.

    The reconfigure step reads the entry's meters from the device registry
    rather than from the coordinator, because the dialog can be opened while
    the entry is not loaded. Creating the device directly is what models that.

    The identifier is built from DEVICE_ID_PREFIX, the same constant sensor.py
    builds the real one from and config_flow.py strips the meter id back out
    of. A literal here would keep producing the old spelling if that prefix
    ever moved, and the step under test would then find no meters at all —
    which is a passing result for some of these tests.
    """
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{DEVICE_ID_PREFIX}{meter_id}")},
        name=f"Water ({meter_id})",
    )


def _schema_default(schema, key: str):
    """Return the default a form field offers, or None if it has none."""
    for marker in schema.schema:
        if marker == key:
            return marker.default() if marker.default is not vol.UNDEFINED else None
    raise AssertionError(f"{key} missing from schema")


# --- validate_input itself -------------------------------------------------
#
# Every other test in this file replaces validate_input() with a stub, which is
# right for testing the dialog around it but left the function itself with no
# coverage at all: the error mapping, the title and the client close could all
# have been deleted with nothing turning red.


async def test_validate_input_returns_the_entry_title(
    hass: HomeAssistant, mock_brunata_client
):
    """The title is what the user sees in the integrations list, and it is the
    only place the address is shown once setup is done."""
    result = await validate_input(hass, CREDENTIALS)

    assert result == {"title": "Brunata (test@example.com)"}
    mock_brunata_client.async_validate_credentials.assert_awaited_once()


@pytest.mark.parametrize(
    ("api_error", "flow_error"),
    [
        (BrunataAuthError("credentials rejected"), InvalidAuth),
        (BrunataConnectionError("network unreachable"), CannotConnect),
        (BrunataApiError("Brunata returned 500"), CannotConnect),
    ],
)
async def test_validate_input_maps_api_errors_to_flow_errors(
    hass: HomeAssistant, mock_brunata_client, api_error, flow_error
):
    """A network problem reported as a bad password sends people off changing
    credentials that were fine. The three API error types are the whole reason
    this mapping exists, and none of them reached it before."""
    mock_brunata_client.async_validate_credentials = AsyncMock(side_effect=api_error)

    with pytest.raises(flow_error):
        await validate_input(hass, CREDENTIALS)


@pytest.mark.parametrize(
    "side_effect", [None, BrunataAuthError("credentials rejected")]
)
async def test_validate_input_always_closes_the_client(
    hass: HomeAssistant, mock_brunata_client, side_effect
):
    """The client owns an httpx session. Without the close, every login
    attempt — successful or not — leaks one for the life of the process, and a
    user retyping a password leaks one per attempt."""
    mock_brunata_client.async_validate_credentials = AsyncMock(side_effect=side_effect)

    try:
        await validate_input(hass, CREDENTIALS)
    except InvalidAuth:
        pass

    mock_brunata_client.async_close.assert_awaited_once()


# --- The user step ---------------------------------------------------------
#
# validate_input() is stubbed throughout: the real one builds an API client and
# performs a live login, and it has its own tests above. What is under test
# here is the dialog around it — which step is shown, what is stored, and what
# the user is told when the login does not go through.

_VALIDATE_INPUT = "custom_components.brunata.config_flow.validate_input"

# What validate_input() returns for CREDENTIALS. Spelled out rather than
# imported from _entry_title(), so a change to the title format has to be
# agreed with the test above that asserts the format itself, instead of every
# stub in this file silently following it.
ENTRY_TITLE = "Brunata (test@example.com)"


async def test_the_dialog_opens_on_the_credentials_form(hass: HomeAssistant):
    """Adding the integration asks for credentials and nothing else.

    The step id is what decides which translation strings the frontend shows,
    so it is asserted rather than taken on trust.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "user"
    assert not result.get("errors")


async def test_accepted_credentials_are_stored_on_a_new_entry(
    hass: HomeAssistant, mock_brunata_client
):
    """A login that goes through ends the dialog and keeps what was typed.

    The stored data is asserted in full: the entry is the only place the
    password lives, and the integration cannot log in again without it.
    """
    form = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    with patch(_VALIDATE_INPUT, return_value={"title": ENTRY_TITLE}):
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"], dict(CREDENTIALS)
        )
        await hass.async_block_till_done()

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["title"] == ENTRY_TITLE
    assert result["data"] == CREDENTIALS


@pytest.mark.parametrize(
    ("raised", "shown"),
    [
        (InvalidAuth, "invalid_auth"),
        (CannotConnect, "cannot_connect"),
        (RuntimeError, "unknown"),
    ],
)
async def test_a_rejected_login_leaves_the_form_open_with_a_reason(
    hass: HomeAssistant, mock_brunata_client, raised, shown
):
    """Each failure has to come back as its own message on the same form.

    All three paths matter and none of them were covered before. A connection
    problem reported as "invalid_auth" sends the user off changing a password
    that was never wrong, and an unexpected error reported as either of the
    other two hides a bug behind advice that cannot help.
    """
    form = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    with patch(_VALIDATE_INPUT, side_effect=raised):
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"], dict(CREDENTIALS)
        )
        await hass.async_block_till_done()

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": shown}


# --- Re-authentication ------------------------------------------------------


async def test_reauth_replaces_the_stored_password(
    hass: HomeAssistant, mock_brunata_client
):
    """Re-authenticating writes the new password onto the existing entry.

    The entry is reloaded on success, which is why the coordinator's update is
    stubbed as well: without it the reload performs a real refresh and the
    assertions below race it.

    Asserted on the entry rather than on the flow result, because storing the
    password is the entire point of the step — an abort with
    "reauth_successful" that left the old password behind would look like a
    success and fail on the next poll.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=CREDENTIALS["email"],
        data={"email": CREDENTIALS["email"], "password": "expired-password"},
    )
    entry.add_to_hass(hass)

    form = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
    )
    assert form["type"] == data_entry_flow.FlowResultType.FORM
    assert form["step_id"] == "reauth_confirm"

    with (
        patch(_VALIDATE_INPUT, return_value={"title": ENTRY_TITLE}),
        patch(
            "custom_components.brunata.BrunataDataUpdateCoordinator._async_update_data",
            return_value={},
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"], dict(CREDENTIALS)
        )
        await hass.async_block_till_done()

    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["password"] == CREDENTIALS["password"]


async def test_flow_reauth_invalid_auth(hass: HomeAssistant, mock_brunata_client):
    """Test that invalid credentials during reauth show an error and keep the old data."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="test@example.com",
        data={
            "email": "test@example.com",
            "password": "old_password",
        },
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
    )

    with patch(
        "custom_components.brunata.config_flow.validate_input",
        side_effect=InvalidAuth,
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "email": "test@example.com",
                "password": "still_wrong",
            },
        )
        await hass.async_block_till_done()

    assert result2["type"] == data_entry_flow.FlowResultType.FORM
    assert result2["step_id"] == "reauth_confirm"
    assert result2["errors"] == {"base": "invalid_auth"}
    assert entry.data["password"] == "old_password"


async def test_flow_reauth_cannot_connect(hass: HomeAssistant, mock_brunata_client):
    """Test that a connection error during reauth is shown as cannot_connect, not unknown."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="test@example.com",
        data={
            "email": "test@example.com",
            "password": "old_password",
        },
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
    )

    with patch(
        "custom_components.brunata.config_flow.validate_input",
        side_effect=CannotConnect,
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "email": "test@example.com",
                "password": "new_password123",
            },
        )
        await hass.async_block_till_done()

    assert result2["type"] == data_entry_flow.FlowResultType.FORM
    assert result2["errors"] == {"base": "cannot_connect"}


async def test_flow_reauth_wrong_account(hass: HomeAssistant, mock_brunata_client):
    """Test that reauthenticating with a different email aborts instead of
    silently reassigning the config entry to a different Brunata account."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="test@example.com",
        data={
            "email": "test@example.com",
            "password": "old_password",
        },
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
    )

    with patch(
        "custom_components.brunata.config_flow.validate_input",
        return_value={"title": "other@example.com"},
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "email": "other@example.com",
                "password": "new_password123",
            },
        )
        await hass.async_block_till_done()

    assert result2["type"] == data_entry_flow.FlowResultType.ABORT
    assert result2["reason"] == "wrong_account"
    assert entry.data["email"] == "test@example.com"
    assert entry.data["password"] == "old_password"


async def test_reauth_rejects_the_wrong_account_without_logging_in(
    hass: HomeAssistant, mock_brunata_client
):
    """The unique_id check comes before validate_input().

    Both orders end in the same wrong_account abort, so the outcome above does
    not pin the ordering down. Entering a different address is a mistake the
    unique_id already knows about; spending a full Keycloak round trip against
    a bot-protected endpoint to reach the same answer costs the user seconds
    and Brunata a request for nothing.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="test@example.com",
        data={"email": "test@example.com", "password": "old_password"},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
    )

    with patch(
        "custom_components.brunata.config_flow.validate_input",
        return_value={"title": "other@example.com"},
    ) as validate:
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"email": "other@example.com", "password": "new_password123"},
        )
        await hass.async_block_till_done()

    assert result2["reason"] == "wrong_account"
    assert validate.call_count == 0


async def test_credential_fields_use_selectors(hass: HomeAssistant, mock_brunata_client):
    """The password field must render masked, in all three forms that ask for
    one. A bare `str` in the schema gives an ordinary text box, so the password
    was visible while being typed. The reconfigure form is included because it
    is the newest of the three and gained its address field last."""
    def _field(schema, key):
        for marker in schema.schema:
            if marker == key:
                return schema.schema[marker]
        raise AssertionError(f"{key} missing from schema")

    def _assert_credential_fields(schema):
        email = _field(schema, "email")
        password = _field(schema, "password")
        assert isinstance(email, selector.TextSelector)
        assert isinstance(password, selector.TextSelector)
        assert email.config["type"] == selector.TextSelectorType.EMAIL
        assert password.config["type"] == selector.TextSelectorType.PASSWORD
        # Lets password managers offer the stored credentials.
        assert email.config["autocomplete"] == "username"
        assert password.config["autocomplete"] == "current-password"

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["step_id"] == "user"
    _assert_credential_fields(result["data_schema"])

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="test@example.com",
        data={"email": "test@example.com", "password": "old_password"},
    )
    entry.add_to_hass(hass)

    reauth = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
    )
    assert reauth["step_id"] == "reauth_confirm"
    _assert_credential_fields(reauth["data_schema"])

    reconfigure = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    assert reconfigure["step_id"] == "reconfigure"
    _assert_credential_fields(reconfigure["data_schema"])


async def test_flow_user_normalises_email_for_unique_id(
    hass: HomeAssistant, mock_brunata_client
):
    """The unique_id must be case-insensitive, otherwise the same account can
    be added twice with different casing."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="test@example.com",
        data={"email": "test@example.com", "password": "password123"},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    with patch(
        "custom_components.brunata.config_flow.validate_input",
        return_value={"title": "Test@Example.com"},
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "email": "  Test@Example.COM  ",
                "password": "password123",
            },
        )
        await hass.async_block_till_done()

    assert result2["type"] == data_entry_flow.FlowResultType.ABORT
    assert result2["reason"] == "already_configured"


async def test_flow_user_stores_and_logs_in_with_the_normalised_email(
    hass: HomeAssistant, mock_brunata_client
):
    """Normalisation has to reach the credentials and the entry, not just the
    unique_id.

    Storing the raw string meant an address pasted from a password manager as
    "  Bruger@Example.COM " was sent to Keycloak with the whitespace attached:
    the login failed, the user was told the password was wrong, and the entry
    title carried the padding too.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    with patch(
        "custom_components.brunata.config_flow.validate_input",
        return_value={"title": "Brunata (bruger@example.com)"},
    ) as validate:
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "email": "  Bruger@Example.COM ",
                "password": "password123",
            },
        )
        await hass.async_block_till_done()

    assert result2["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result2["data"]["email"] == "bruger@example.com"
    assert validate.call_args.args[1]["email"] == "bruger@example.com"


async def test_reauth_logs_in_with_the_normalised_email(
    hass: HomeAssistant, mock_brunata_client
):
    """Same on the reauth path, where it matters more: the form offers the
    stored address as its default, so a padded one would be re-submitted every
    time the user tried to fix their password."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="bruger@example.com",
        data={"email": "bruger@example.com", "password": "old"},
    )
    entry.add_to_hass(hass)

    reauth = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
    )

    with patch(
        "custom_components.brunata.config_flow.validate_input",
        return_value={"title": "Brunata (bruger@example.com)"},
    ) as validate, patch(
        "custom_components.brunata.BrunataDataUpdateCoordinator._async_update_data",
        return_value={},
    ):
        result = await hass.config_entries.flow.async_configure(
            reauth["flow_id"],
            {
                "email": " Bruger@Example.COM  ",
                "password": "new-password",
            },
        )
        await hass.async_block_till_done()

    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert validate.call_args.args[1]["email"] == "bruger@example.com"
    assert entry.data["email"] == "bruger@example.com"
    assert entry.data["password"] == "new-password"


# --- reconfigure -----------------------------------------------------------


async def test_reconfigure_updates_the_password(
    hass: HomeAssistant, mock_brunata_client
):
    """Changing a password in Brunata Online should not require a failure first.

    Without this step the only route is to wait for the integration to fail on
    the old password and let reauth start — up to an hour of a broken
    integration and a notification about something the user already fixed.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="test@example.com",
        title="Brunata (test@example.com)",
        data={"email": "test@example.com", "password": "old_password"},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    assert result["step_id"] == "reconfigure"
    # The stored address is offered back, so leaving the field alone is a
    # password change and nothing else.
    assert _schema_default(result["data_schema"], "email") == "test@example.com"

    with patch(
        "custom_components.brunata.config_flow.validate_input",
        return_value={"title": "Brunata (test@example.com)"},
    ) as validate:
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"email": "test@example.com", "password": "new_password123"},
        )
        await hass.async_block_till_done()

    assert result2["type"] == data_entry_flow.FlowResultType.ABORT
    assert result2["reason"] == "reconfigure_successful"
    assert entry.data["password"] == "new_password123"
    # The address is unchanged, so a login is still attempted with both halves
    # of the credentials.
    assert validate.call_args.args[1]["email"] == "test@example.com"
    assert entry.unique_id == "test@example.com"


async def test_reconfigure_with_the_same_address_does_not_list_meters(
    hass: HomeAssistant, mock_brunata_client
):
    """An unchanged address is a password change, and a password change must
    stay one round trip.

    The meter listing exists to prove that a *new* address is the same
    household. Running it on every password change would double the traffic
    against a bot-protected endpoint for a question already answered.

    Asserted on the helper rather than on the client's own call counter: a
    successful reconfiguration reloads the entry, and the reload fetches the
    meters as an ordinary update, so the counter cannot say which of the two
    it was counting.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="test@example.com",
        data={"email": "test@example.com", "password": "old_password"},
    )
    entry.add_to_hass(hass)
    _add_meter_device(hass, entry, "12345")

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )

    with patch(
        "custom_components.brunata.config_flow.validate_input",
        return_value={"title": "Brunata (test@example.com)"},
    ), patch(
        "custom_components.brunata.config_flow._async_missing_meters"
    ) as missing_meters:
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"email": "test@example.com", "password": "new_password123"},
        )
        await hass.async_block_till_done()

    assert result2["reason"] == "reconfigure_successful"
    assert missing_meters.call_count == 0


async def test_reconfigure_normalises_an_address_stored_before_1_4_0(
    hass: HomeAssistant, mock_brunata_client
):
    """The stored address is normalised on the way out, not assumed to be clean.

    async_step_user only started normalising what it writes in 1.4.0. Before
    that, only the unique_id derived from the address was cleaned up, so an
    older entry can hold "  Bruger@Example.COM " verbatim. The form offers the
    stored value as its default, so without normalising it here the user would
    submit the padded string back and Keycloak would reject it — and they would
    be told their password was wrong.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="bruger@example.com",
        data={"email": "  Bruger@Example.COM ", "password": "old_password"},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    assert _schema_default(result["data_schema"], "email") == "bruger@example.com"

    with patch(
        "custom_components.brunata.config_flow.validate_input",
        return_value={"title": "Brunata (bruger@example.com)"},
    ) as validate:
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"email": "bruger@example.com", "password": "new_password123"},
        )
        await hass.async_block_till_done()

    assert result2["type"] == data_entry_flow.FlowResultType.ABORT
    assert validate.call_args.args[1]["email"] == "bruger@example.com"
    # And written back, so the entry corrects itself rather than staying odd
    # until someone deletes and re-adds it.
    assert entry.data["email"] == "bruger@example.com"
    assert entry.data["password"] == "new_password123"


async def test_reconfigure_keeps_the_old_password_when_the_new_one_is_wrong(
    hass: HomeAssistant, mock_brunata_client
):
    """A rejected password must not be written to the entry. Otherwise a typo
    replaces working credentials with broken ones and reauth starts on the next
    poll — the exact failure this step exists to avoid."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="test@example.com",
        data={"email": "test@example.com", "password": "old_password"},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )

    with patch(
        "custom_components.brunata.config_flow.validate_input",
        side_effect=InvalidAuth,
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"email": "test@example.com", "password": "wrong_password"},
        )

    assert result2["type"] == data_entry_flow.FlowResultType.FORM
    assert result2["errors"] == {"base": "invalid_auth"}
    assert entry.data["password"] == "old_password"


# --- reconfigure: moving the entry to a new address ------------------------


async def test_reconfigure_moves_the_address_when_the_meters_match(
    hass: HomeAssistant, mock_brunata_client, mock_meter
):
    """The whole point of the step: a Brunata account reached under a new
    e-mail keeps its history.

    Deleting and re-adding the integration is the alternative, and it starts
    every meter's long term statistics from zero. Moving the entry does not,
    because entry_id is what the entity and device registries are keyed on and
    entry_id cannot change.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="gammel@example.com",
        title="Brunata (gammel@example.com)",
        data={"email": "gammel@example.com", "password": "password123"},
    )
    entry.add_to_hass(hass)
    device = _add_meter_device(hass, entry, "12345")
    mock_brunata_client.async_get_meters = AsyncMock(return_value={"12345": mock_meter})

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"email": "  Ny@Example.COM ", "password": "password123"},
    )
    await hass.async_block_till_done()

    assert result2["type"] == data_entry_flow.FlowResultType.ABORT
    assert result2["reason"] == "reconfigure_successful"
    # Written in its normalised form, in all three places that hold it.
    assert entry.unique_id == "ny@example.com"
    assert entry.data["email"] == "ny@example.com"
    assert entry.title == "Brunata (ny@example.com)"
    # And the device the statistics hang off is the one that was there before.
    assert dr.async_get(hass).async_get(device.id) is not None


async def test_reconfigure_refuses_an_address_missing_a_known_meter(
    hass: HomeAssistant, mock_brunata_client, mock_meter
):
    """A partial match is the one case where statistics can actually be lost.

    The identity would move, but a meter whose number the new account does not
    report becomes a new sensor with a new internal id, and the old one is left
    behind without data. The user gets no notice and finds out in the energy
    dashboard months later. So the flow stops instead, and the user still has
    the route they have today: delete the integration and set it up again.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="gammel@example.com",
        data={"email": "gammel@example.com", "password": "password123"},
    )
    entry.add_to_hass(hass)
    _add_meter_device(hass, entry, "12345")
    _add_meter_device(hass, entry, "67890")
    # The new account reports only one of the two.
    mock_brunata_client.async_get_meters = AsyncMock(return_value={"12345": mock_meter})

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"email": "ny@example.com", "password": "password123"},
    )
    await hass.async_block_till_done()

    assert result2["type"] == data_entry_flow.FlowResultType.ABORT
    assert result2["reason"] == "meter_mismatch"
    # Nothing was written on the way to the abort.
    assert entry.unique_id == "gammel@example.com"
    assert entry.data["email"] == "gammel@example.com"


async def test_reconfigure_counts_a_meter_with_an_unresolved_unit_as_present(
    hass: HomeAssistant, mock_brunata_client, mock_meter
):
    """A meter Brunata reported but whose unit code did not resolve is still on
    the wall.

    api/parse.py drops it from the parsed result so it cannot become an entity
    carrying a raw number as its unit, and it names it in the parse report
    instead. Reading only the parsed meters here would call it gone and block a
    move that was legitimate.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="gammel@example.com",
        data={"email": "gammel@example.com", "password": "password123"},
    )
    entry.add_to_hass(hass)
    _add_meter_device(hass, entry, "12345")
    _add_meter_device(hass, entry, "67890")
    mock_brunata_client.async_get_meters = AsyncMock(return_value={"12345": mock_meter})
    mock_brunata_client.last_parse_report = MagicMock(
        return_value=ParseReport(frozenset({"67890"}), 2)
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"email": "ny@example.com", "password": "password123"},
    )
    await hass.async_block_till_done()

    assert result2["reason"] == "reconfigure_successful"
    assert entry.unique_id == "ny@example.com"


async def test_reconfigure_refuses_an_address_another_entry_already_has(
    hass: HomeAssistant, mock_brunata_client
):
    """Home Assistant does not stop a unique_id from being reused.

    It writes the value, logs "Unique id of config entry ... changed to ...
    which is already in use", and leaves two entries claiming the same account.
    Nothing in this integration can undo that, so the address is checked
    against the other entries before anything is written — and before the
    login, since the answer needs no network at all.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="gammel@example.com",
        data={"email": "gammel@example.com", "password": "password123"},
    )
    entry.add_to_hass(hass)
    other = MockConfigEntry(
        domain=DOMAIN,
        unique_id="optaget@example.com",
        data={"email": "optaget@example.com", "password": "password123"},
    )
    other.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"email": "optaget@example.com", "password": "password123"},
    )
    await hass.async_block_till_done()

    assert result2["type"] == data_entry_flow.FlowResultType.ABORT
    assert result2["reason"] == "account_already_configured"
    assert entry.unique_id == "gammel@example.com"
    assert mock_brunata_client.async_get_meters.await_count == 0
    assert mock_brunata_client.async_validate_credentials.await_count == 0


async def test_reconfigure_moving_still_checks_the_password(
    hass: HomeAssistant, mock_brunata_client
):
    """An entry with no devices yet has no meters to compare, and that must not
    turn into "accept whatever was typed".

    The meter listing is also the login. Skipping it when there is nothing to
    compare would write an unverified password to the entry, and the next poll
    would open the re-authentication dialog for a change the user was just told
    had succeeded.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="gammel@example.com",
        data={"email": "gammel@example.com", "password": "password123"},
    )
    entry.add_to_hass(hass)
    mock_brunata_client.async_get_meters = AsyncMock(
        side_effect=BrunataAuthError("credentials rejected")
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"email": "ny@example.com", "password": "wrong_password"},
    )

    assert result2["type"] == data_entry_flow.FlowResultType.FORM
    assert result2["errors"] == {"base": "invalid_auth"}
    assert entry.unique_id == "gammel@example.com"
    assert entry.data["password"] == "password123"


async def test_reconfigure_shows_cannot_connect_while_moving(
    hass: HomeAssistant, mock_brunata_client
):
    """A network problem during the meter listing must not read as a bad
    password, for the same reason it must not on the other two paths: it sends
    people off changing credentials that were fine."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="gammel@example.com",
        data={"email": "gammel@example.com", "password": "password123"},
    )
    entry.add_to_hass(hass)
    _add_meter_device(hass, entry, "12345")
    mock_brunata_client.async_get_meters = AsyncMock(
        side_effect=BrunataConnectionError("network unreachable")
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"email": "ny@example.com", "password": "password123"},
    )

    assert result2["type"] == data_entry_flow.FlowResultType.FORM
    assert result2["errors"] == {"base": "cannot_connect"}
    assert entry.unique_id == "gammel@example.com"


async def test_reconfigure_closes_the_client_after_listing_meters(
    hass: HomeAssistant, mock_brunata_client, mock_meter
):
    """The meter listing builds its own client, which owns an httpx session.
    Without the close, every attempt at moving an address leaks one for the
    life of the process.

    The address is refused here on purpose. A successful move reloads the
    entry, the reload builds a second client of its own, and the counter could
    then no longer say anything about the one the listing owned.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="gammel@example.com",
        data={"email": "gammel@example.com", "password": "password123"},
    )
    entry.add_to_hass(hass)
    _add_meter_device(hass, entry, "12345")
    _add_meter_device(hass, entry, "67890")
    mock_brunata_client.async_get_meters = AsyncMock(return_value={"12345": mock_meter})

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"email": "ny@example.com", "password": "password123"},
    )
    await hass.async_block_till_done()

    assert result2["reason"] == "meter_mismatch"
    assert mock_brunata_client.async_close.await_count == 1

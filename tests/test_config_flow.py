"""Test Brunata config flow."""
from unittest.mock import patch

from homeassistant import config_entries, data_entry_flow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import selector
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.brunata.config_flow import CannotConnect, InvalidAuth
from custom_components.brunata.const import DOMAIN


async def test_flow_user_init(hass: HomeAssistant):
    """Test the initialization of the form in the config flow."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "user"


async def test_flow_user_success(hass: HomeAssistant, mock_brunata_client):
    """Test a successful user login."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    with patch(
        "custom_components.brunata.config_flow.validate_input",
        return_value={"title": "test@example.com"},
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "email": "test@example.com",
                "password": "password123",
            },
        )
        await hass.async_block_till_done()

    assert result2["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result2["title"] == "test@example.com"
    assert result2["data"] == {
        "email": "test@example.com",
        "password": "password123",
    }


async def test_flow_user_invalid_auth(hass: HomeAssistant, mock_brunata_client):
    """Test invalid authentication handling."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    with patch(
        "custom_components.brunata.config_flow.validate_input",
        side_effect=InvalidAuth,
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "email": "test@example.com",
                "password": "wrong_password",
            },
        )
        await hass.async_block_till_done()

    assert result2["type"] == data_entry_flow.FlowResultType.FORM
    assert result2["errors"] == {"base": "invalid_auth"}


async def test_flow_reauth(hass: HomeAssistant, mock_brunata_client):
    """Test a successful re-authentication updates and reloads the entry."""
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
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    with patch(
        "custom_components.brunata.config_flow.validate_input",
        return_value={"title": "test@example.com"},
    ), patch(
        "custom_components.brunata.BrunataDataUpdateCoordinator._async_update_data",
        return_value={},
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "email": "test@example.com",
                "password": "new_password123",
            },
        )
        await hass.async_block_till_done()

    assert result2["type"] == data_entry_flow.FlowResultType.ABORT
    assert result2["reason"] == "reauth_successful"
    assert entry.data["password"] == "new_password123"


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
    """The password field must render masked, in both the initial and the
    reauth form. A bare `str` in the schema gives an ordinary text box, so the
    password was visible while being typed."""
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
        data={"email": "test@example.com", "password": "old_password"},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    assert result["step_id"] == "reconfigure"

    # The address is not on the form: it is the unique_id, and changing it
    # would orphan every entity and device behind it.
    assert "email" not in result["data_schema"].schema

    with patch(
        "custom_components.brunata.config_flow.validate_input",
        return_value={"title": "Brunata (test@example.com)"},
    ) as validate:
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"password": "new_password123"},
        )
        await hass.async_block_till_done()

    assert result2["type"] == data_entry_flow.FlowResultType.ABORT
    assert result2["reason"] == "reconfigure_successful"
    assert entry.data["password"] == "new_password123"
    # The stored address is reused, so a login is still attempted with both
    # halves of the credentials.
    assert validate.call_args.args[1]["email"] == "test@example.com"


async def test_reconfigure_normalises_an_address_stored_before_1_4_0(
    hass: HomeAssistant, mock_brunata_client
):
    """The stored address is normalised on the way out, not assumed to be clean.

    async_step_user only started normalising what it writes in 1.4.0. Before
    that, only the unique_id derived from the address was cleaned up, so an
    older entry can hold "  Bruger@Example.COM " verbatim. Reconfigure logs in
    with the stored value, so without normalising it here Keycloak rejects the
    padded string and the user is told their password is wrong — the exact
    failure normalisation was added to prevent, on the one path that was added
    in the same release and never got it.
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
    # The dialog says which account is being changed, so it must not offer the
    # padded string either.
    assert result["description_placeholders"]["email"] == "bruger@example.com"

    with patch(
        "custom_components.brunata.config_flow.validate_input",
        return_value={"title": "Brunata (bruger@example.com)"},
    ) as validate:
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"password": "new_password123"},
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
            {"password": "wrong_password"},
        )

    assert result2["type"] == data_entry_flow.FlowResultType.FORM
    assert result2["errors"] == {"base": "invalid_auth"}
    assert entry.data["password"] == "old_password"

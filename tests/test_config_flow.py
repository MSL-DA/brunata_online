"""Test Brunata config flow."""
import logging
from unittest.mock import AsyncMock, patch
from homeassistant import config_entries, data_entry_flow
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from custom_components.brunata.const import DOMAIN, CONF_DEBUG_LOGGING

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
    from custom_components.brunata.config_flow import InvalidAuth

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
    from custom_components.brunata.config_flow import InvalidAuth

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
    from custom_components.brunata.config_flow import CannotConnect

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

async def test_options_flow_reloads_entry_and_applies_debug_logging(
    hass: HomeAssistant, mock_brunata_client
):
    """Saving the options form should reload the config entry automatically
    (BrunataOptionsFlowHandler subclasses OptionsFlowWithReload — see
    https://developers.home-assistant.io/docs/core/integration/options_flow/#options-flow-with-automatic-reload)
    instead of relying on a hand-rolled entry.add_update_listener(), and the
    resulting reload should pick up the new debug-logging option."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "email": "test@example.com",
            "password": "password123",
        },
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.brunata._check_connectivity",
        AsyncMock(return_value=True),
    ), patch(
        "custom_components.brunata.BrunataDataUpdateCoordinator._async_update_data",
        return_value={},
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] == data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "init"

        result2 = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_DEBUG_LOGGING: True},
        )
        await hass.async_block_till_done()

    assert result2["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    # OptionsFlowWithReload triggers the reload itself; a fully successful
    # reload leaves the entry loaded again and, per async_setup_entry,
    # having picked up the new option.
    assert entry.state is config_entries.ConfigEntryState.LOADED
    assert logging.getLogger("custom_components.brunata").level == logging.DEBUG
    assert logging.getLogger("brunata_api").level == logging.DEBUG


async def test_options_flow_turns_debug_logging_back_off(
    hass: HomeAssistant, mock_brunata_client
):
    """Disabling the option must reset the log level. Setting only the DEBUG
    case would leave both loggers stuck at DEBUG until the next restart."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "email": "test@example.com",
            "password": "password123",
        },
        options={CONF_DEBUG_LOGGING: True},
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.brunata._check_connectivity",
        AsyncMock(return_value=True),
    ), patch(
        "custom_components.brunata.BrunataDataUpdateCoordinator._async_update_data",
        return_value={},
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert logging.getLogger("brunata_api").level == logging.DEBUG

        result = await hass.config_entries.options.async_init(entry.entry_id)
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_DEBUG_LOGGING: False},
        )
        await hass.async_block_till_done()

    assert logging.getLogger("custom_components.brunata").level == logging.NOTSET
    assert logging.getLogger("brunata_api").level == logging.NOTSET


async def test_credential_fields_use_selectors(hass: HomeAssistant, mock_brunata_client):
    """The password field must render masked, in both the initial and the
    reauth form. A bare `str` in the schema gives an ordinary text box, so the
    password was visible while being typed."""
    from homeassistant.helpers import selector

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

"""Tests for the FedEx config and options flow."""

from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from homeassistant.config_entries import SOURCE_USER
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.fedex.api import (
    FedExApiError,
    FedExAuthError,
)
from custom_components.fedex.config_flow import (
    normalize_tracking_code,
    valid_tracking_code,
)
from custom_components.fedex.const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_INCLUDE_HISTORY,
    CONF_PARCELS,
    CONF_TRACKING_CODE,
    DOMAIN,
)

KEY_INPUT = {CONF_CLIENT_ID: "test-client-id", CONF_CLIENT_SECRET: "test-client-secret"}

VALIDATE_KEY = "custom_components.fedex.config_flow.FedExApiClient.async_validate_key"


def _entry(parcels: list[dict] | None = None) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data=KEY_INPUT,
        options={
            CONF_PARCELS: parcels or [],
            CONF_DELIVERED_FILTER_TYPE: "days",
            CONF_DELIVERED_FILTER_AMOUNT: 7,
            CONF_INCLUDE_HISTORY: False,
        },
    )


def test_normalize_tracking_code_strips_and_uppercases():
    assert normalize_tracking_code("example 123-456") == "EXAMPLE123456"
    assert normalize_tracking_code("") == ""
    assert normalize_tracking_code(None) == ""


def test_valid_tracking_code_accepts_any_non_empty_code():
    assert valid_tracking_code("EXAMPLE123456")
    assert not valid_tracking_code("")


# ---------------------------------------------------------------------------
# user step
# ---------------------------------------------------------------------------


async def test_user_flow_creates_hub_with_a_valid_key(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["step_id"] == "user"

    with patch(VALIDATE_KEY, new=AsyncMock(return_value=None)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], KEY_INPUT
        )

    assert result["type"] == "create_entry"
    assert result["title"] == "FedEx"
    assert result["data"] == KEY_INPUT
    assert result["options"][CONF_PARCELS] == []


@pytest.mark.parametrize(
    "error,expected",
    [
        (FedExAuthError("HTTP 401"), "invalid_auth"),
        (FedExApiError("HTTP 500"), "cannot_connect"),
        (aiohttp.ClientError("boom"), "cannot_connect"),
    ],
)
async def test_user_flow_surfaces_errors(hass, error, expected):
    """A rejected key and an outage must not look the same to the user."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch(VALIDATE_KEY, new=AsyncMock(side_effect=error)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], KEY_INPUT
        )

    assert result["type"] == "form"
    assert result["errors"] == {"base": expected}


async def test_second_hub_rejected(hass):
    _entry().add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] == "abort"
    # single_config_entry in the manifest aborts before the flow runs.
    assert result["reason"] == "single_instance_allowed"


# ---------------------------------------------------------------------------
# reauth
# ---------------------------------------------------------------------------


async def test_reauth_updates_the_key(hass):
    entry = _entry()
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    with patch(VALIDATE_KEY, new=AsyncMock(return_value=None)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_CLIENT_ID: "new-client", CONF_CLIENT_SECRET: "new-secret"},
        )
        await hass.async_block_till_done()

    assert result["type"] == "abort"
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_CLIENT_ID] == "new-client"


async def test_reauth_surfaces_invalid_key(hass):
    entry = _entry()
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    with patch(VALIDATE_KEY, new=AsyncMock(side_effect=FedExAuthError("HTTP 401"))):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], KEY_INPUT
        )

    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_auth"}


async def test_reauth_surfaces_connection_errors(hass):
    entry = _entry()
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    with patch(VALIDATE_KEY, new=AsyncMock(side_effect=FedExApiError("HTTP 500"))):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], KEY_INPUT
        )

    assert result["errors"] == {"base": "cannot_connect"}


# ---------------------------------------------------------------------------
# options -- identical to the account-less build; the key does not affect how
# parcels are tracked
# ---------------------------------------------------------------------------


async def _open_options_step(hass, entry, step_id: str):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "menu"
    assert result["menu_options"] == ["parcels", "settings"]
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": step_id}
    )


async def test_options_add_parcel(hass):
    entry = _entry()
    entry.add_to_hass(hass)

    result = await _open_options_step(hass, entry, "parcels")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tracking_codes": ["example123456"]}
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_PARCELS] == [{CONF_TRACKING_CODE: "EXAMPLE123456"}]


async def test_options_remove_parcel(hass):
    entry = _entry(
        [
            {CONF_TRACKING_CODE: "EXAMPLE111111"},
            {CONF_TRACKING_CODE: "EXAMPLE222222"},
        ]
    )
    entry.add_to_hass(hass)
    result = await _open_options_step(hass, entry, "parcels")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tracking_codes": ["EXAMPLE222222"]}
    )
    assert result["type"] == "create_entry"
    codes = {p[CONF_TRACKING_CODE] for p in result["data"][CONF_PARCELS]}
    assert codes == {"EXAMPLE222222"}


async def test_options_changes_history_and_delivered(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    result = await _open_options_step(hass, entry, "settings")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 5,
            CONF_INCLUDE_HISTORY: True,
        },
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_INCLUDE_HISTORY] is True
    assert result["data"][CONF_DELIVERED_FILTER_TYPE] == "parcels"
    assert result["data"][CONF_DELIVERED_FILTER_AMOUNT] == 5
